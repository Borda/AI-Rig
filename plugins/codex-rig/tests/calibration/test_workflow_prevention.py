"""Exercise bounded workflow counterexamples through the shipped calibration checks.

The calibration runner (`runtime/calibration/run.py`) decides whether policy text, public summaries and helper modules
still reject known workflow regressions. These tests feed it counterexamples, degraded helpers and tampered review
artifacts, and check both the findings it names and that its result file is still written.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from _fixture_files import file_sha256
from _modules import load_module_scoped

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

_SHARED_HELPERS = ("escalation_ledger", "adversarial_loop")

_LIGHTWEIGHT = """## Lightweight Local Work
Use parent-owned implementation for one bounded change. Routing examples are optional; tests and documentation for one change do not create separate domains.
Define a concise plan. Perform a parent self-challenge (not independent). Accept the challenged plan under existing authority. Assign execution to the parent. Make the authorized change. Consolidate the actual result. Obtain one bounded independent challenge of the changed behavior. Verify core acceptance.
"""

#: Joiners between a historical clause and a following current clause on the same line.
_CLAUSE_SEPARATORS = [
    "; ",
    ", ",
    ", but ",
    " but ",
    ", while ",
    " while ",
    ", whereas ",
    " whereas ",
    ", however, ",
    " however ",
    ", and ",
    " and ",
]

_HISTORY_LABELS = ["Historical", "Archived"]

_CASE_VARIANTS = [pytest.param(str, id="as-written"), pytest.param(str.lower, id="lowercased")]

_SCHEMA_6_CLAIM = "New native specialist manifests use schema 6"


@pytest.fixture(scope="module")
def runner() -> Iterator[ModuleType]:
    """Load calibration once per module through the repository's standalone import contract.

    The 3.7k-line runner keeps no mutable module state (all run state lives in `CalibrationRun`), so one immutable
    import is shared instead of re-executing it for every case.
    """
    with pytest.MonkeyPatch.context() as patch:
        yield load_module_scoped("workflow_prevention_calibration", PLUGIN_ROOT / "runtime/calibration/run.py", patch)


@pytest.fixture
def make_run(runner: ModuleType, tmp_path: Path) -> Callable[..., object]:
    """Build calibration runs rooted in `tmp_path` with selected `Paths` fields replaced."""

    def _make(kind: str = "plugin", **path_overrides: object) -> object:
        return runner.CalibrationRun(replace(runner.Paths.create(kind, tmp_path), **path_overrides))

    return _make


def _write_files(directory: Path, files: dict[str, str]) -> Path:
    """Create `directory` and write each `name -> text` file into it."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8", newline="\n")
    return directory


def _shipped_helper(name: str) -> str:
    """Return the shipped source of one shared workflow helper."""
    return (PLUGIN_ROOT / "shared" / f"{name}.py").read_text(encoding="utf-8")


def _stage_shared(directory: Path, overrides: dict[str, str]) -> Path:
    """Copy the shipped workflow helpers into `directory/shared`, replacing those named in `overrides`."""
    sources = {name: _shipped_helper(name) for name in _SHARED_HELPERS}
    return _write_files(directory / "shared", {f"{name}.py": text for name, text in {**sources, **overrides}.items()})


def _finish(runner: ModuleType, run: object) -> dict[str, object]:
    """Write the calibration result file and return its parsed content."""
    runner.write_result(run)
    return json.loads(run.paths.result.read_bytes())


def _leaks(run: object) -> str:
    """Return the text of the calibration leak report."""
    return run.paths.leaks.read_text(encoding="utf-8")


def _plugin_tree(root: Path, agents_text: str) -> tuple[Path, Path]:
    """Create a minimal plugin with an AGENTS asset and a lightweight implement skill; return (plugin, skills)."""
    plugin = root / "plugin"
    skills = plugin / "skills"
    _write_files(plugin / "assets", {"AGENTS.md": agents_text})
    _write_files(skills / "implement", {"SKILL.md": _LIGHTWEIGHT})
    return plugin, skills


class TestBenchmarkWordingDiagnostics:
    """Punctuation-only benchmark wording hints are advisory, structural gaps still fail."""

    @pytest.mark.parametrize(
        ("skill_text", "expected_failed", "expected_leaks"),
        [
            pytest.param("## Workflow\nPreserve identity, ownership and destination.\n", [], 0, id="structure-present"),
            pytest.param(
                "Preserve identity, ownership and destination.\n",
                ["native-skill-contract"],
                1,
                id="structure-missing",
            ),
        ],
    )
    def test_hint_stays_visible_without_hiding_missing_structure(
        self, runner, make_run, tmp_path, skill_text, expected_failed, expected_leaks
    ) -> None:
        """A wording hint is recorded as advisory and only a missing heading counts as a failed check.

        The skill says "identity, ownership and destination" while the benchmark expects the Oxford comma. That must
        surface as an advisory miss and never block a structurally valid skill, yet a skill lacking the workflow heading
        still fails its structural check.
        """
        skill = tmp_path / "skills" / "audit" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(skill_text, encoding="utf-8")
        benchmarks = tmp_path / "benchmarks.json"
        benchmarks.write_text(
            json.dumps({"skills": {"audit": ["identity, ownership, and destination"]}}), encoding="utf-8"
        )
        run = make_run(skills_dir=tmp_path / "skills", benchmarks=benchmarks)

        runner.run_benchmark_pattern_checks(run)
        runner.check_contains(run, skill, "^## Workflow$", "native-skill-contract")

        assert run.checks_failed == expected_failed
        assert run.fails == 0
        assert run.leaks == expected_leaks
        assert "advisory:benchmark-skill-miss:audit:" in run.paths.checks.read_text(encoding="utf-8")


class TestLightweightLocalWork:
    """The lightweight route keeps parent-owned work ordered and independently challenged."""

    def test_accepts_parent_owned_work_with_independent_challenge(self, runner) -> None:
        """The updated route is recognised without requiring the parent to perform its own challenge."""
        assert runner.workflow_policy_findings("", _LIGHTWEIGHT) == []

    @pytest.mark.parametrize(
        "missing",
        [
            "one bounded independent challenge",
            "concise plan",
            "parent self-challenge",
            "Accept the challenged plan",
            "Assign execution",
            "Consolidate",
        ],
    )
    def test_rejects_missing_stage(self, runner, missing: str) -> None:
        """Dropping any preparation stage or the independent challenge makes the route incomplete.

        A result review cannot retroactively replace a missing preparation stage, and a route that silently omits the
        mandatory independent challenge is incomplete.
        """
        assert "lightweight-local-work" in runner.workflow_policy_findings("", _LIGHTWEIGHT.replace(missing, "omitted"))

    def test_rejects_reversed_preparation(self, runner) -> None:
        """A self-challenge recorded only after the source change is rejected."""
        reversed_order = _LIGHTWEIGHT.replace("Perform a parent self-challenge (not independent). ", "").replace(
            "Make the authorized change.",
            "Make the authorized change. Perform a parent self-challenge (not independent).",
        )

        assert "lightweight-local-work" in runner.workflow_policy_findings("", reversed_order)

    @pytest.mark.parametrize(
        "implementation",
        [
            pytest.param("", id="empty-implementation"),
            pytest.param(_LIGHTWEIGHT.replace("parent-owned", "delegated"), id="parent-branch-removed"),
            pytest.param(
                _LIGHTWEIGHT + "\nAlways require sw-engineer and qa-specialist for every task.",
                id="mandatory-specialists",
            ),
        ],
    )
    def test_rejects_branch_mutations(self, runner, implementation: str) -> None:
        """An explicit parent branch is required and mandatory specialist routing is refused."""
        assert "lightweight-local-work" in runner.workflow_policy_findings("", implementation)


class TestWorkflowPolicyFindings:
    """Policy text is checked for obsolete runtime models and blanket review stops."""

    @pytest.mark.parametrize(
        ("policy", "expected"),
        [
            pytest.param(
                'Normal runtime model = "gpt-5.6-terra"', "obsolete-runtime-model", id="obsolete-model-assignment"
            ),
            pytest.param(
                "Implementation roles use GPT-5.6 at high.", "obsolete-runtime-model", id="obsolete-prose-routing"
            ),
            pytest.param(
                'Runtime model = "gpt-5.6-terra" # archived evidence',
                "obsolete-runtime-model",
                id="archive-word-in-assignment",
            ),
            pytest.param(
                "An open structural finding stops a clean claim.", "blanket-review-stop", id="structural-stop"
            ),
            pytest.param(
                "The same open signature in consecutive reviews stops a clean claim.",
                "blanket-review-stop",
                id="repeated-signature-stop",
            ),
        ],
    )
    def test_rejects_known_regressions(self, runner, policy: str, expected: str) -> None:
        """Known policy regressions are named rather than accepting executable helpers alone."""
        assert expected in runner.workflow_policy_findings(policy, _LIGHTWEIGHT)

    def test_allows_historical_model_evidence(self, runner) -> None:
        """Explicitly historical references stay out of runtime routing findings."""
        policy = "Historical GPT-5.6 routing evidence remains archived."

        assert runner.workflow_policy_findings(policy, _LIGHTWEIGHT) == []

    @pytest.mark.parametrize("label", _HISTORY_LABELS)
    @pytest.mark.parametrize(
        "rule",
        [
            "same open signature in consecutive reviews stops a clean claim",
            "open structural finding stops a clean claim",
        ],
    )
    @pytest.mark.parametrize(
        ("template", "transform", "expected"),
        [
            pytest.param(
                "{label} policy: {rule}. Current continuation accepts decreasing scores.", str, [], id="historical-only"
            ),
            pytest.param(
                "{label} policy: {rule}. Current continuation accepts decreasing scores.",
                str.lower,
                [],
                id="historical-only-lowercased",
            ),
            pytest.param(
                "{label} policy: {rule}. Current continuation accepts decreasing scores. Current policy: {rule}.",
                str,
                ["blanket-review-stop"],
                id="active-sentence-after-history",
            ),
            pytest.param(
                "{label} policy: {rule}, but current policy: {rule}.",
                str,
                ["blanket-review-stop"],
                id="active-clause-after-history",
            ),
        ],
    )
    def test_historical_stop_quotation_keeps_active_checks(
        self, runner, label: str, rule: str, template: str, transform, expected: list[str]
    ) -> None:
        """An archived stop quotation is exempt while a current blanket stop in the same policy is still rejected.

        The quotation may appear in its own sentence or clause, in any letter case. Only a separately stated current
        stop restores the finding.
        """
        policy = transform(template.format(label=label, rule=rule))

        assert runner.workflow_policy_findings(policy, _LIGHTWEIGHT) == expected

    @pytest.mark.parametrize("label", _HISTORY_LABELS)
    @pytest.mark.parametrize("separator", _CLAUSE_SEPARATORS)
    @pytest.mark.parametrize("transform", _CASE_VARIANTS)
    def test_historical_clause_keeps_active_model_visible(self, runner, label: str, separator: str, transform) -> None:
        """Explicit current routing after a separately labeled historical clause is rejected.

        The archived GPT-5.6 clause and the current obsolete-model assignment share one line; no joiner between them may
        let the history label mask the current assignment.
        """
        policy = f'{label} GPT-5.6 observations remain archived{separator}current runtime model = "gpt-5.6-terra".'

        assert "obsolete-runtime-model" in runner.workflow_policy_findings(transform(policy), _LIGHTWEIGHT)

    @pytest.mark.parametrize("label", _HISTORY_LABELS)
    def test_allows_historical_clause_alone(self, runner, label: str) -> None:
        """A labeled historical clause with no current assignment yields no findings."""
        policy = f"{label} GPT-5.6 observations remain archived."

        assert runner.workflow_policy_findings(policy, _LIGHTWEIGHT) == []

    @pytest.mark.parametrize("label", _HISTORY_LABELS)
    @pytest.mark.parametrize("separator", _CLAUSE_SEPARATORS)
    def test_allows_supported_model_after_historical_clause(self, runner, label: str, separator: str) -> None:
        """Replacing the current assignment with a supported model clears the finding."""
        policy = f'{label} GPT-5.6 observations remain archived{separator}current runtime model = "gpt-6.1-sol".'

        assert runner.workflow_policy_findings(policy, _LIGHTWEIGHT) == []

    @pytest.mark.parametrize("transform", _CASE_VARIANTS)
    def test_mixed_historical_and_current_model_is_flagged(self, runner, transform) -> None:
        """Sentence-scoped history does not hide a current obsolete model in the same paragraph."""
        policy = 'Historical GPT-5.6 routing remains archived. Current runtime model = "gpt-5.6-terra".'

        assert "obsolete-runtime-model" in runner.workflow_policy_findings(transform(policy), _LIGHTWEIGHT)


class TestWorkflowSummaryFindings:
    """Public summaries must not contradict productive progress, parent-only routing or the native schema."""

    @pytest.mark.parametrize(
        ("summary", "expected"),
        [
            pytest.param(
                "Three attempts require escalation regardless of their progress flags.",
                "productive-summary-contradiction",
                id="useful-work-forced-stop",
            ),
            pytest.param(
                "Any planned parent mutation requires separate approval record.",
                "write-summary-contradiction",
                id="serial-parent-forced-approval",
            ),
            pytest.param(
                "Every write still needs newly frozen plan and exact digest-bound approval.",
                "write-summary-contradiction",
                id="all-writes-forced-dispatch-plan",
            ),
            pytest.param(
                "On a repeated scope, prior open weighted findings must fall by at least half for the loop to continue.",
                "review-half-score-contradiction",
                id="challenge-summary-stops-useful-reduction",
            ),
            pytest.param(
                "If that old-finding residue exceeds half the prior open score, stop under the plateau gate.",
                "review-half-score-contradiction",
                id="challenge-consumer-stops-useful-reduction",
            ),
            pytest.param(
                "Code Review also has separate instruction-bounded native inspection route: reviewers are instructed not to use child tools or repository execution.",
                "native-review-delivery-contradiction",
                id="current-native-route-forbids-required-reader",
            ),
            pytest.param(
                f"{_SCHEMA_6_CLAIM} with a verified frozen-context read.",
                "native-review-delivery-contradiction",
                id="obsolete-current-native-schema",
            ),
        ],
    )
    def test_rejects_known_contradictions(self, runner, summary: str, expected: str) -> None:
        """Known public summaries that contradict productive and parent-only routing are named."""
        assert expected in runner.workflow_summary_findings(summary)

    def test_allows_generic_write_guard_and_historical_evidence(self, runner) -> None:
        """Generic dispatch approvals and historical model evidence in summaries are accepted."""
        summary = (
            "For generic write promotion, approval is separate and applies to every write. Historical GPT-5.6 evidence "
            "remains archived. Useful primary progress resets the failure count; authorized parent-only work proceeds."
        )

        assert runner.workflow_summary_findings(summary) == []

    @pytest.mark.parametrize(
        ("summary", "expected"),
        [
            pytest.param(
                f"Historical policy: {_SCHEMA_6_CLAIM}. Current manifests use schema 8.", [], id="historical-only"
            ),
            pytest.param(
                f"Historical policy: {_SCHEMA_6_CLAIM}. Current manifests use schema 8. {_SCHEMA_6_CLAIM}.",
                ["native-review-delivery-contradiction"],
                id="current-claim-after-history",
            ),
            pytest.param(
                f"historical policy: {_SCHEMA_6_CLAIM.lower()}. current manifests use schema 8. {_SCHEMA_6_CLAIM.lower()}.",
                ["native-review-delivery-contradiction"],
                id="current-claim-after-history-lowercased",
            ),
            pytest.param(
                f"{_SCHEMA_6_CLAIM}, based on archived evidence.",
                ["native-review-delivery-contradiction"],
                id="archive-word-in-same-clause",
            ),
        ],
    )
    def test_historical_schema_summary_scope(self, runner, summary: str, expected: list[str]) -> None:
        """Historical evidence is exempt at sentence scope rather than masking a whole mixed paragraph.

        An archived schema-6 statement is fine, but repeating the claim as current, or merely mentioning archived
        evidence inside the same clause, must still be reported.
        """
        assert runner.workflow_summary_findings(summary) == expected

    @pytest.mark.parametrize("label", _HISTORY_LABELS)
    @pytest.mark.parametrize("separator", _CLAUSE_SEPARATORS)
    @pytest.mark.parametrize("transform", _CASE_VARIANTS)
    def test_historical_clause_keeps_active_claim_visible(self, runner, label: str, separator: str, transform) -> None:
        """A current schema claim stays visible after an archived clause on the same line."""
        summary = f"{label} policy: {_SCHEMA_6_CLAIM}{separator}{_SCHEMA_6_CLAIM}."

        assert runner.workflow_summary_findings(transform(summary)) == ["native-review-delivery-contradiction"]

    @pytest.mark.parametrize("label", _HISTORY_LABELS)
    @pytest.mark.parametrize("separator", _CLAUSE_SEPARATORS)
    def test_allows_current_schema_after_historical_clause(self, runner, label: str, separator: str) -> None:
        """Restating the clause with the current schema 8 after an archived clause is accepted."""
        summary = f"{label} policy: {_SCHEMA_6_CLAIM}{separator}New native specialist manifests use schema 8."

        assert runner.workflow_summary_findings(summary) == []

    @pytest.mark.parametrize("label", _HISTORY_LABELS)
    def test_allows_historical_schema_before_current_sentence(self, runner, label: str) -> None:
        """A historical schema-6 sentence followed by a separate current schema-8 sentence is accepted."""
        summary = f"{label} policy: {_SCHEMA_6_CLAIM}. Current manifests use schema 8."

        assert runner.workflow_summary_findings(summary) == []


class TestOutcomeChecks:
    """Workflow outcome checks accept productive ledgers and reject mutated helper branches."""

    @pytest.mark.integration
    def test_productive_ledger_and_incremental_review_compose(self, runner, make_run) -> None:
        """Productive review outcomes and the ordinary benchmark pattern checks agree.

        Runs the real shared helpers through the outcome and pattern checks together and expects every documented
        outcome line, including the incremental review score progression, to be recorded as ok.
        """
        run = make_run()
        expected_lines = [
            "workflow-outcome:productive-primary=ok",
            "workflow-outcome:productive-recovery=ok",
            "workflow-outcome:recovery-followed-by-one-failure=ok",
            "workflow-outcome:recovery-followed-by-stall-handoff=ok",
            "workflow-outcome:incremental-review=ok:6->5",
            "workflow-outcome:review-progress-bound=ok:6->5->1->0",
        ]

        runner.check_workflow_outcomes(run)
        runner.run_benchmark_pattern_checks(run)

        checks = run.paths.checks.read_text(encoding="utf-8")
        assert run.checks_failed == [], _leaks(run) if run.paths.leaks.exists() else ""
        assert [line for line in expected_lines if line not in checks] == []

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("helper", "before", "after", "expected"),
        [
            pytest.param(
                "escalation_ledger",
                'if cycle["material_progress"]:\n            failed_attempts = 0',
                "if False:\n            failed_attempts = 0",
                "productive-primary",
                id="productive-attempts-counted-as-failures",
            ),
            pytest.param(
                "escalation_ledger",
                'if outcome not in {"working", "closed", "human_handoff"}:',
                'if outcome == "working":',
                "productive-recovery",
                id="productive-recovery-demands-handoff",
            ),
            pytest.param(
                "adversarial_loop",
                "elif score == previous_score:",
                "elif score >= previous_score * 0.75:",
                "incremental-review",
                id="small-score-drop-rejected",
            ),
        ],
    )
    def test_rejects_helper_mutations(self, runner, make_run, tmp_path, helper, before, after, expected) -> None:
        """Calibration rejects the actual anti-productivity branches when a shipped helper is mutated.

        Each case rewrites one line of the real helper source in a private copy and expects the matching workflow
        outcome check to fail, proving the check observes helper behaviour and not just the helper's presence.
        """
        source = _shipped_helper(helper)
        assert source.count(before) == 1
        run = make_run(shared_dir=_stage_shared(tmp_path, {helper: source.replace(before, after)}))

        runner.check_workflow_outcomes(run)

        assert "workflow-prevention" in run.checks_failed
        assert f"workflow-outcome:{expected}:" in _leaks(run)

    def test_shipped_behavioral_case_rejects_false_progress_escalation(self, runner) -> None:
        """The shipped behavioral oracle agrees with productive helper outcomes.

        The `model-stall-progress-without-closure` case lists the findings a false escalation must produce and must
        satisfy the runner's own contract validator.
        """
        cases = json.loads((PLUGIN_ROOT / "runtime/calibration/behavioral-cases.json").read_text(encoding="utf-8"))
        case = next(case for case in cases["cases"] if case["id"] == "model-stall-progress-without-closure")

        runner.validate_model_stall_case_contract(cases)

        assert case["expected_findings"] == [
            "productive-progress-misclassified",
            "false-advisory-escalation",
            "unfinished-acceptance-must-remain-recorded",
        ]


@pytest.mark.integration
class TestDegradedHelpersKeepResult:
    """A broken or degraded helper is reported as a failure while the result file is still written."""

    @pytest.mark.parametrize(
        ("files", "expected_code"),
        [
            pytest.param({}, "workflow-helper-unavailable", id="missing"),
            pytest.param(
                {"escalation_ledger.py": "def invalid(\n"}, "workflow-helper-import-invalid", id="syntax-error"
            ),
        ],
    )
    def test_unimportable_helper_records_failure(self, runner, make_run, tmp_path, files, expected_code) -> None:
        """A missing or unparsable helper fails the check and leaves the diagnostic artifact available.

        The shared directory holds no `adversarial_loop` helper and either no `escalation_ledger` or one with a syntax
        error; both helpers must be named in the leak report and the result status must be `fail`.
        """
        run = make_run(shared_dir=_write_files(tmp_path / "shared", files))

        runner.check_workflow_outcomes(run)
        result = _finish(runner, run)

        assert result["status"] == "fail"
        assert "workflow-prevention" in result["checks_failed"]
        assert "workflow-helper-unavailable:adversarial_loop" in _leaks(run)
        assert f"{expected_code}:escalation_ledger" in _leaks(run)

    @pytest.mark.parametrize(
        ("helper", "source", "missing_export"),
        [
            pytest.param(
                "escalation_ledger",
                '"""Degraded helper."""\n',
                "SCHEMA_VERSION",
                id="missing-schema-with-intact-review-helper",
            ),
            pytest.param(
                "escalation_ledger",
                "SCHEMA_VERSION = 3\n",
                "validate_ledger",
                id="missing-validator-with-intact-review-helper",
            ),
            pytest.param(
                "escalation_ledger",
                "SCHEMA_VERSION = 3\nvalidate_ledger = None\n",
                "validate_ledger",
                id="noncallable-validator",
            ),
            pytest.param(
                "adversarial_loop",
                '"""Degraded helper."""\n',
                "summarize_ledger",
                id="missing-summary-with-intact-progress-helper",
            ),
            pytest.param("adversarial_loop", "summarize_ledger = None\n", "summarize_ledger", id="noncallable-summary"),
        ],
    )
    def test_importable_helper_missing_api(self, runner, make_run, tmp_path, helper, source, missing_export) -> None:
        """A helper that imports but lacks a required export is named in the leak report.

        The sibling helper stays intact so the failure is attributed to the degraded helper alone; the result file must
        still be written with status `fail`.
        """
        run = make_run(shared_dir=_stage_shared(tmp_path, {helper: source}))

        runner.check_workflow_outcomes(run)
        result = _finish(runner, run)

        assert result["status"] == "fail"
        assert f"workflow-helper-api-invalid:{helper}:{missing_export}" in _leaks(run)

    @pytest.mark.parametrize("source", ['"""Degraded helper."""\n', "_write_input_snapshot = None\n"])
    def test_live_snapshot_helper_missing_api(self, runner, make_run, tmp_path, source: str) -> None:
        """The report is preserved when the live runner imports but cannot snapshot its inputs."""
        degraded = tmp_path / "live-runner.py"
        degraded.write_text(source, encoding="utf-8")
        run = make_run(live_ab_runner=degraded)
        fixture = runner._live_ab_contract_fixture(run, tmp_path / "live-selftest")

        passed = runner.selftest_live_ab_contract_input_snapshot(run, fixture)
        result = _finish(runner, run)

        assert passed is False
        assert result["status"] == "fail"
        assert "selftest-api:live-ab-runner:_write_input_snapshot" in _leaks(run)

    @pytest.mark.parametrize("source", ['"""Degraded helper."""\n', "_validate_code_remediate_pr_identity = None\n"])
    def test_source_validator_missing_api(self, runner, make_run, tmp_path, source: str) -> None:
        """Source-layout diagnostics survive when the identity helper's required API is absent."""
        degraded = tmp_path / "validator.py"
        degraded.write_text(source, encoding="utf-8")
        run = make_run("source", validate_artifacts=degraded)

        runner.selftest_code_remediate_pr_identity(run)
        result = _finish(runner, run)

        assert result["status"] == "fail"
        assert "selftest-api:code-remediate-pr-identity:_validate_code_remediate_pr_identity" in _leaks(run)

    @pytest.mark.parametrize(
        "validator_source",
        [
            pytest.param("import nonexistent_calibration_dependency\n", id="missing-dependency"),
            pytest.param("def invalid(\n", id="syntax-error"),
        ],
    )
    def test_source_layout_validator_import_failure(self, runner, make_run, tmp_path, validator_source: str) -> None:
        """A downstream source selftest does not discard the earlier missing-helper diagnosis."""
        validator = tmp_path / "validate-artifacts.py"
        validator.write_text(validator_source, encoding="utf-8")
        run = make_run("source", validate_artifacts=validator)

        runner.selftest_code_remediate_pr_identity(run)
        result = _finish(runner, run)

        assert result["status"] == "fail"
        assert "selftest-import:code-remediate-pr-identity" in _leaks(run)


@pytest.mark.integration
class TestComposedPreventionCheck:
    """`check_workflow_prevention` reads shipped summaries and source policy together."""

    @pytest.mark.parametrize("document", ["README.md"])
    def test_summary_mutation_fails(self, runner, make_run, tmp_path, document: str) -> None:
        """A contradictory shipped summary document fails the composed calibration gate.

        The plugin summary claims escalation regardless of progress flags; both the README and the architecture document
        are read as part of the existing composed check.
        """
        plugin, skills = _plugin_tree(tmp_path, "Current runtime uses GPT-6.1.\n")
        summary = _write_files(plugin, {document: "Escalate regardless of their progress flags.\n"}) / document
        run = make_run(asset_root=plugin, skills_dir=skills)

        runner.check_workflow_prevention(run)

        assert f"workflow-summary:productive-summary-contradiction:{summary}" in _leaks(run)

    def test_source_policy_mutation_fails(self, runner, make_run, tmp_path) -> None:
        """An obsolete runtime model in the session policy fails the composed calibration gate.

        The current source policy is read in composition rather than relying on helper outcomes alone: only the
        policy check fails while the helper outcome checks still record their ok lines.
        """
        plugin, skills = _plugin_tree(tmp_path, "Use GPT-6.1 for runtime work.\n")
        source = _write_files(tmp_path / ".codex", {"global-session-policy.md": 'Runtime model = "gpt-5.6-terra"\n'})
        run = make_run(asset_root=plugin, skills_dir=skills)

        runner.check_workflow_prevention(run)

        assert run.checks_failed == ["workflow-prevention"]
        assert f"workflow-policy:obsolete-runtime-model:{source / 'global-session-policy.md'}" in _leaks(run)
        assert "workflow-outcome:incremental-review=ok:6->5" in run.paths.checks.read_text(encoding="utf-8")


def _write_json(path: Path, data: object) -> None:
    """Write `data` as one JSON line."""
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def _pin_plan(out: Path, plan: dict[str, object]) -> None:
    """Write `plan` and re-hash the execution and specialist manifests that pin it, like a hostile rehash."""
    plan_path = out / "execution-plan.json"
    execution_path = out / "execution-manifest.json"
    manifest_path = out / "specialist-manifest.json"
    _write_json(plan_path, plan)
    execution = json.loads(execution_path.read_bytes())
    execution["plan_sha256"] = file_sha256(plan_path)
    _write_json(execution_path, execution)
    manifest = json.loads(manifest_path.read_bytes())
    manifest["runtime_execution"]["manifest_sha256"] = file_sha256(execution_path)
    _write_json(manifest_path, manifest)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """Write `rows` as JSON lines."""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.mark.integration
class TestReviewValidatorSelftest:
    """The review validator accepts neutral runtimes and rejects tampered provenance (real validator subprocess)."""

    def test_accepts_neutral_runtime(self, runner, make_run, tmp_path) -> None:
        """The selftest passes for a model-neutral runtime and for its provenance tampering checks.

        A current portable runtime previously failed because the consumer looked for a model in the role card instead of
        the frozen dispatch plan. The selftest also checks that altered reviewer identity and model do not pass.
        """
        run = make_run()

        runner.selftest_review_validator(run, tmp_path / "review-selftest")

        assert run.checks_failed == []

    def test_accepts_windows_newlines(self, runner, make_run, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Git patch bytes stay portable when native text writes use CRLF.

        `Path.write_text` is patched to default to CRLF newlines, as a Windows host would, before the selftest builds
        its patch fixtures.
        """
        run = make_run()
        write_text = Path.write_text

        def windows_write(path, data, encoding=None, errors=None, newline=None):
            return write_text(
                path, data, encoding=encoding, errors=errors, newline="\r\n" if newline is None else newline
            )

        monkeypatch.setattr(Path, "write_text", windows_write)

        runner.selftest_review_validator(run, tmp_path / "review-selftest")

        assert run.checks_failed == []

    def test_rejects_rehashed_runtime_model_change(self, runner, make_run, tmp_path) -> None:
        """A rehashed frozen model choice cannot override the actual specialist spawn.

        Starting from a validated selftest, the plan's model is changed and every pinning hash is recomputed; the
        validator must still compare against the spawn record. Dropping the spawn model is accepted only when the parent
        turn context supplies the same model, and an explicit mismatch is rejected again.
        """
        run = make_run()
        root = tmp_path / "review-selftest"
        runner.selftest_review_validator(run, root)
        out = root / "review-validator"
        command = [
            sys.executable,
            run.paths.code_review_validate_artifacts,
            "--out",
            out,
            "--result",
            out / "result.json",
            "--codex-home",
            out / "codex-home",
            "--project-root",
            run.paths.root,
            "--parent-thread-id",
            "parent-selftest",
        ]
        baseline = runner.run_command(command)
        plan = json.loads((out / "execution-plan.json").read_bytes())
        plan["dispatch_choices"]["qa-specialist"]["model"] = "different-test-model"
        _pin_plan(out, plan)

        changed_model = runner.run_command(command)

        plan["dispatch_choices"]["qa-specialist"]["model"] = None
        _pin_plan(out, plan)
        parent_path = out / "codex-home" / "sessions" / "rollout-parent-selftest.jsonl"
        rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
        spawn = next(row for row in rows if row.get("payload", {}).get("name") == "spawn_agent")
        arguments = json.loads(spawn["payload"]["arguments"])
        arguments.pop("model")
        spawn["payload"]["arguments"] = json.dumps(arguments)
        rows.insert(1, {"type": "turn_context", "payload": {"turn_id": "parent-turn", "model": runner.DEFAULT_MODEL}})
        _write_jsonl(parent_path, rows)
        inherited = runner.run_command(command)
        arguments["model"] = runner.DEFAULT_MODEL
        spawn["payload"]["arguments"] = json.dumps(arguments)
        _write_jsonl(parent_path, rows)
        explicit_mismatch = runner.run_command(command)

        mismatch = "review-inspection-spawn-model-selection-mismatch:qa-specialist"
        assert run.checks_failed == []
        assert baseline.returncode == 0
        assert changed_model.returncode != 0
        assert mismatch in changed_model.stderr
        assert inherited.returncode == 0, inherited.stderr
        assert explicit_mismatch.returncode != 0
        assert mismatch in explicit_mismatch.stderr


def _without_models(route: dict[str, object]) -> dict[str, object]:
    """Return a route with its baseline and candidate model ids removed."""
    return {key: value for key, value in route.items() if key not in {"baseline_model", "candidate_model"}}


class TestLiveGuardSelftests:
    """Live A/B guard selftests use only synthetic current-model policy and never rewrite archived evidence."""

    @pytest.fixture
    def guard(self, runner, make_run, tmp_path) -> tuple[object, Path, Path]:
        """Return (run, live_dir, synthetic policy path) for a fresh guard selftest."""
        run = make_run()
        live_dir = tmp_path / "guard-selftest"
        return run, live_dir, runner.synthetic_live_guard_policy(run, live_dir)

    def test_synthetic_policy_swaps_only_model_ids(self, guard) -> None:
        """The synthetic policy replaces model ids and keeps every other route field of the archived policy.

        Supported test models must be plannable without editing archived evidence, so the archived policy file stays
        byte-identical.
        """
        run, _live_dir, policy_path = guard
        archived_bytes = run.paths.live_route_policy.read_bytes()
        archived = json.loads(archived_bytes)

        synthetic = json.loads(policy_path.read_bytes())

        assert policy_path.name == "synthetic-guard-policy.json"
        assert all(
            route["baseline_model"] == "baseline-test-model" and route["candidate_model"] == "candidate-test-model"
            for route in synthetic["routes"].values()
        )
        assert {route_id: _without_models(route) for route_id, route in synthetic["routes"].items()} == {
            route_id: _without_models(archived["routes"][route_id]) for route_id in synthetic["routes"]
        }
        assert run.paths.live_route_policy.read_bytes() == archived_bytes

    def test_plan_and_guards_reach_ci_and_api_blocks(self, runner, guard) -> None:
        """The synthetic policy reaches the plan, CI and API-key guards and writes no run output.

        Each selftest must record its documented check line, leave the archived policy untouched, and clean up any
        directory it created.
        """
        run, live_dir, policy_path = guard
        archived_bytes = run.paths.live_route_policy.read_bytes()
        expected_lines = [
            "selftest:live-ab-synthetic-plan=64-calls:unconfirmed:no-output",
            "selftest:live-ab-synthetic-ci-guard=blocked:no-output",
            "selftest:live-ab-synthetic-api-key-guard=blocked:no-output",
            "selftest:live-ab-archived-policy=ci-blocked:no-output",
        ]

        plan_ok = runner.selftest_live_ab_contract_plan(run, live_dir, policy_path)
        paid_guards_ok = runner.selftest_live_ab_contract_paid_guards(run, live_dir, policy_path)
        archived_guard_ok = runner.selftest_live_ab_contract_archived_policy_guard(run, live_dir)

        checks = run.paths.checks.read_text(encoding="utf-8")
        assert plan_ok
        assert paid_guards_ok
        assert archived_guard_ok
        assert run.checks_failed == []
        assert [line for line in expected_lines if line not in checks] == []
        assert run.paths.live_route_policy.read_bytes() == archived_bytes
        assert not any(
            (live_dir / name).exists()
            for name in ("planned-run", "ci-blocked-run", "api-key-blocked-run", "archive-blocked-run")
        )
        assert runner._live_ab_contract_fixture(run, live_dir).policy_payload == json.loads(archived_bytes)
