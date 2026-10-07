"""Exercise bounded workflow counterexamples through the shipped calibration checks."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _runner() -> ModuleType:
    """Load calibration through the repository's existing standalone import contract."""
    spec = importlib.util.spec_from_file_location(
        "workflow_prevention_calibration", PLUGIN_ROOT / "runtime/calibration/run.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_LIGHTWEIGHT = """## Lightweight Local Work
Use parent-only execution for one bounded change. Routing examples are optional; tests and documentation for one change do not create separate domains.
"""


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
        pytest.param("An open structural finding stops a clean claim.", "blanket-review-stop", id="structural-stop"),
        pytest.param(
            "The same open signature in consecutive reviews stops a clean claim.",
            "blanket-review-stop",
            id="repeated-signature-stop",
        ),
    ],
)
def test_policy_counterexamples_are_rejected(policy: str, expected: str) -> None:
    """Reject the known policy regressions rather than accepting executable helpers alone."""
    assert expected in _runner().workflow_policy_findings(policy, _LIGHTWEIGHT)


def test_historical_model_evidence_is_allowed() -> None:
    """Keep explicitly historical references out of runtime routing findings."""
    assert (
        _runner().workflow_policy_findings("Historical GPT-5.6 routing evidence remains archived.", _LIGHTWEIGHT) == []
    )


@pytest.mark.parametrize("label", ["Historical", "Archived"])
@pytest.mark.parametrize(
    "rule",
    ["same open signature in consecutive reviews stops a clean claim", "open structural finding stops a clean claim"],
)
def test_historical_stop_quotation_preserves_active_policy_checks(label: str, rule: str) -> None:
    """Exempt archived stop quotations while rejecting current blanket stops in the same policy."""
    runner = _runner()
    historical = f"{label} policy: {rule}. Current continuation accepts decreasing scores."
    assert runner.workflow_policy_findings(historical, _LIGHTWEIGHT) == []
    assert runner.workflow_policy_findings(historical.lower(), _LIGHTWEIGHT) == []
    active = historical + f" Current policy: {rule}."
    assert runner.workflow_policy_findings(active, _LIGHTWEIGHT) == ["blanket-review-stop"]
    mixed_clause = f"{label} policy: {rule}, but current policy: {rule}."
    assert runner.workflow_policy_findings(mixed_clause, _LIGHTWEIGHT) == ["blanket-review-stop"]


@pytest.mark.parametrize(
    "implementation",
    [
        "",
        pytest.param(_LIGHTWEIGHT.replace("parent-only", "delegated"), id="parent-branch-removed"),
        pytest.param(
            _LIGHTWEIGHT + "\nAlways require sw-engineer and qa-specialist for every task.", id="mandatory-specialists"
        ),
    ],
)
def test_lightweight_branch_mutations_are_rejected(implementation: str) -> None:
    """Require an explicit parent branch without mandatory specialist routing."""
    assert "lightweight-local-work" in _runner().workflow_policy_findings("", implementation)


@pytest.mark.integration
def test_productive_ledger_and_incremental_review_compose(tmp_path: Path) -> None:
    """Keep productive review outcomes and their ordinary calibration checks aligned."""
    runner = _runner()
    paths = runner.Paths.create("plugin", tmp_path)
    run = runner.CalibrationRun(paths)
    runner.check_workflow_outcomes(run)
    runner.run_benchmark_pattern_checks(run)
    assert run.checks_failed == [], paths.leaks.read_text(encoding="utf-8") if paths.leaks.exists() else ""
    checks = paths.checks.read_text(encoding="utf-8")
    assert "workflow-outcome:productive-primary=ok" in checks
    assert "workflow-outcome:productive-recovery=ok" in checks
    assert "workflow-outcome:recovery-followed-by-one-failure=ok" in checks
    assert "workflow-outcome:recovery-followed-by-stall-handoff=ok" in checks
    assert "workflow-outcome:incremental-review=ok:6->5" in checks
    assert "workflow-outcome:review-progress-bound=ok:6->5->1->0" in checks


@pytest.mark.parametrize("broken_helper", ["missing", "syntax-error"])
@pytest.mark.integration
def test_broken_helper_records_failure_and_writes_result(tmp_path: Path, broken_helper: str) -> None:
    """Keep calibration's diagnostic artifact available when a helper cannot import."""
    runner = _runner()
    shared = tmp_path / "shared"
    shared.mkdir()
    if broken_helper == "syntax-error":
        (shared / "escalation_ledger.py").write_text("def invalid(\n", encoding="utf-8")
    paths = replace(runner.Paths.create("plugin", tmp_path), shared_dir=shared)
    run = runner.CalibrationRun(paths)
    runner.check_workflow_outcomes(run)
    runner.write_result(run)
    result = json.loads(paths.result.read_bytes())
    assert result["status"] == "fail"
    assert "workflow-prevention" in result["checks_failed"]
    leaks = paths.leaks.read_text(encoding="utf-8")
    assert "workflow-helper-unavailable:adversarial_loop" in leaks
    expected = "workflow-helper-unavailable" if broken_helper == "missing" else "workflow-helper-import-invalid"
    assert f"{expected}:escalation_ledger" in leaks


@pytest.mark.integration
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
def test_importable_workflow_helper_missing_api_keeps_result(
    tmp_path: Path, helper: str, source: str, missing_export: str
) -> None:
    """Validate required APIs after successful import with the sibling helper intact."""
    runner = _runner()
    shared = tmp_path / "shared"
    shared.mkdir()
    for name in ("escalation_ledger", "adversarial_loop"):
        (shared / f"{name}.py").write_text(
            source if name == helper else (PLUGIN_ROOT / "shared" / f"{name}.py").read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
    paths = replace(runner.Paths.create("plugin", tmp_path), shared_dir=shared)
    run = runner.CalibrationRun(paths)
    runner.check_workflow_outcomes(run)
    runner.write_result(run)
    assert json.loads(paths.result.read_bytes())["status"] == "fail"
    assert f"workflow-helper-api-invalid:{helper}:{missing_export}" in paths.leaks.read_text(encoding="utf-8")


@pytest.mark.integration
@pytest.mark.parametrize("source", ['"""Degraded helper."""\n', "_write_input_snapshot = None\n"])
def test_importable_live_snapshot_helper_missing_api_keeps_result(tmp_path: Path, source: str) -> None:
    """Preserve the report when the live runner imports but cannot snapshot inputs."""
    runner = _runner()
    degraded = tmp_path / "live-runner.py"
    degraded.write_text(source, encoding="utf-8")
    paths = replace(runner.Paths.create("plugin", tmp_path), live_ab_runner=degraded)
    run = runner.CalibrationRun(paths)
    fixture = runner._live_ab_contract_fixture(run, tmp_path / "live-selftest")
    assert runner.selftest_live_ab_contract_input_snapshot(run, fixture) is False
    runner.write_result(run)
    assert json.loads(paths.result.read_bytes())["status"] == "fail"
    assert "selftest-api:live-ab-runner:_write_input_snapshot" in paths.leaks.read_text(encoding="utf-8")


@pytest.mark.integration
@pytest.mark.parametrize("source", ['"""Degraded helper."""\n', "_validate_code_remediate_pr_identity = None\n"])
def test_importable_source_validator_missing_api_keeps_result(tmp_path: Path, source: str) -> None:
    """Preserve source-layout diagnostics when the identity helper's required API is absent."""
    runner = _runner()
    degraded = tmp_path / "validator.py"
    degraded.write_text(source, encoding="utf-8")
    paths = replace(runner.Paths.create("source", tmp_path), validate_artifacts=degraded)
    run = runner.CalibrationRun(paths)
    runner.selftest_code_remediate_pr_identity(run)
    runner.write_result(run)
    assert json.loads(paths.result.read_bytes())["status"] == "fail"
    assert "selftest-api:code-remediate-pr-identity:_validate_code_remediate_pr_identity" in paths.leaks.read_text(
        encoding="utf-8"
    )


def test_historical_native_schema_summary_does_not_mask_current_statement() -> None:
    """Exempt historical evidence at sentence scope rather than hiding a whole mixed paragraph."""
    runner = _runner()
    historical = "Historical policy: New native specialist manifests use schema 6. Current manifests use schema 8."
    assert runner.workflow_summary_findings(historical) == []
    active = historical + " New native specialist manifests use schema 6."
    assert runner.workflow_summary_findings(active) == ["native-review-delivery-contradiction"]
    assert runner.workflow_summary_findings(active.lower()) == ["native-review-delivery-contradiction"]
    assert runner.workflow_summary_findings(
        "New native specialist manifests use schema 6, based on archived evidence."
    ) == ["native-review-delivery-contradiction"]
    mixed_model = 'Historical GPT-5.6 routing remains archived. Current runtime model = "gpt-5.6-terra".'
    assert "obsolete-runtime-model" in runner.workflow_policy_findings(mixed_model, _LIGHTWEIGHT)
    assert "obsolete-runtime-model" in runner.workflow_policy_findings(mixed_model.lower(), _LIGHTWEIGHT)


@pytest.mark.parametrize("label", ["Historical", "Archived"])
@pytest.mark.parametrize(
    "separator",
    [
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
    ],
)
def test_historical_clause_does_not_mask_active_model(label: str, separator: str) -> None:
    """Reject explicit current routing after a separately labeled historical clause."""
    policy = f'{label} GPT-5.6 observations remain archived{separator}current runtime model = "gpt-5.6-terra".'
    runner = _runner()
    assert "obsolete-runtime-model" in runner.workflow_policy_findings(policy, _LIGHTWEIGHT)
    assert "obsolete-runtime-model" in runner.workflow_policy_findings(policy.lower(), _LIGHTWEIGHT)
    assert runner.workflow_policy_findings(f"{label} GPT-5.6 observations remain archived.", _LIGHTWEIGHT) == []
    current = policy.replace('model = "gpt-5.6-terra"', 'model = "gpt-6.1-sol"')
    assert runner.workflow_policy_findings(current, _LIGHTWEIGHT) == []


@pytest.mark.parametrize("label", ["Historical", "Archived"])
@pytest.mark.parametrize(
    "separator",
    [
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
    ],
)
def test_historical_clause_does_not_mask_active_summary(label: str, separator: str) -> None:
    """Keep a current schema claim visible after an archived clause on the same line."""
    summary = f"{label} policy: New native specialist manifests use schema 6{separator}New native specialist manifests use schema 6."
    runner = _runner()
    assert runner.workflow_summary_findings(summary) == ["native-review-delivery-contradiction"]
    assert runner.workflow_summary_findings(summary.lower()) == ["native-review-delivery-contradiction"]
    current = summary.rsplit("schema 6", 1)[0] + "schema 8."
    assert runner.workflow_summary_findings(current) == []
    assert (
        runner.workflow_summary_findings(
            f"{label} policy: New native specialist manifests use schema 6. Current manifests use schema 8."
        )
        == []
    )


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
            "New native specialist manifests use schema 6 with a verified frozen-context read.",
            "native-review-delivery-contradiction",
            id="obsolete-current-native-schema",
        ),
    ],
)
def test_summary_counterexamples_are_rejected(summary: str, expected: str) -> None:
    """Catch known public summaries that contradict productive and parent-only routing."""
    assert expected in _runner().workflow_summary_findings(summary)


def test_generic_write_guard_and_historical_summary_are_allowed() -> None:
    """Preserve generic dispatch approvals and historical model evidence in summaries."""
    summary = "For generic write promotion, approval is separate and applies to every write. Historical GPT-5.6 evidence remains archived. Useful primary progress resets the failure count; authorized parent-only work proceeds."
    assert _runner().workflow_summary_findings(summary) == []


@pytest.mark.integration
@pytest.mark.parametrize("broken_dependency", ["missing", "syntax-error"])
def test_source_layout_validator_import_failure_keeps_result(tmp_path: Path, broken_dependency: str) -> None:
    """A downstream source selftest must not discard the earlier missing-helper diagnosis."""
    runner = _runner()
    validator = tmp_path / "validate-artifacts.py"
    validator.write_text(
        "import nonexistent_calibration_dependency\n" if broken_dependency == "missing" else "def invalid(\n",
        encoding="utf-8",
    )
    paths = replace(runner.Paths.create("source", tmp_path), validate_artifacts=validator)
    run = runner.CalibrationRun(paths)
    runner.selftest_code_remediate_pr_identity(run)
    runner.write_result(run)

    assert json.loads(paths.result.read_bytes())["status"] == "fail"
    assert "selftest-import:code-remediate-pr-identity" in paths.leaks.read_text(encoding="utf-8")


@pytest.mark.parametrize("document", ["README.md", "ARCHITECTURE.md"])
@pytest.mark.integration
def test_summary_source_mutation_fails_composed_check(tmp_path: Path, document: str) -> None:
    """Read shipped summaries as part of the existing composed calibration gate."""
    runner = _runner()
    plugin = tmp_path / "plugin"
    (plugin / "assets").mkdir(parents=True)
    (plugin / "assets/AGENTS.md").write_text("Current runtime uses GPT-6.1.\n", encoding="utf-8")
    skills = plugin / "skills"
    (skills / "implement").mkdir(parents=True)
    (skills / "implement/SKILL.md").write_text(_LIGHTWEIGHT, encoding="utf-8")
    summary = plugin / document
    summary.write_text("Escalate regardless of their progress flags.\n", encoding="utf-8")
    paths = replace(runner.Paths.create("plugin", tmp_path), asset_root=plugin, skills_dir=skills)
    run = runner.CalibrationRun(paths)
    runner.check_workflow_prevention(run)
    assert f"workflow-summary:productive-summary-contradiction:{summary}" in paths.leaks.read_text(encoding="utf-8")


@pytest.mark.integration
def test_source_policy_mutation_fails_composed_calibration(tmp_path: Path) -> None:
    """Read current source policy in composition rather than relying on helper outcomes alone."""
    runner = _runner()
    plugin = tmp_path / "plugin"
    (plugin / "assets").mkdir(parents=True)
    (plugin / "assets/AGENTS.md").write_text("Use GPT-6.1 for runtime work.\n", encoding="utf-8")
    skills = plugin / "skills"
    (skills / "implement").mkdir(parents=True)
    (skills / "implement/SKILL.md").write_text(_LIGHTWEIGHT, encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    source = tmp_path / ".codex/global-session-policy.md"
    source.write_text('Runtime model = "gpt-5.6-terra"\n', encoding="utf-8")
    paths = replace(runner.Paths.create("plugin", tmp_path), asset_root=plugin, skills_dir=skills)
    run = runner.CalibrationRun(paths)
    runner.check_workflow_prevention(run)
    assert run.checks_failed == ["workflow-prevention"]
    assert f"workflow-policy:obsolete-runtime-model:{source}" in paths.leaks.read_text(encoding="utf-8")
    assert "workflow-outcome:incremental-review=ok:6->5" in paths.checks.read_text(encoding="utf-8")


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
@pytest.mark.integration
def test_outcome_checks_reject_helper_mutations(
    tmp_path: Path, helper: str, before: str, after: str, expected: str
) -> None:
    """Prove calibration rejects the actual anti-productivity helper branches."""
    runner = _runner()
    shared = tmp_path / "shared"
    shared.mkdir()
    for name in ("escalation_ledger", "adversarial_loop"):
        source = (PLUGIN_ROOT / "shared" / f"{name}.py").read_text(encoding="utf-8")
        if name == helper:
            assert source.count(before) == 1
            source = source.replace(before, after)
        (shared / f"{name}.py").write_text(source, encoding="utf-8", newline="\n")
    paths = replace(runner.Paths.create("plugin", tmp_path), shared_dir=shared)
    run = runner.CalibrationRun(paths)
    runner.check_workflow_outcomes(run)
    assert "workflow-prevention" in run.checks_failed
    assert f"workflow-outcome:{expected}:" in paths.leaks.read_text(encoding="utf-8")


def test_shipped_behavioral_case_rejects_false_progress_escalation() -> None:
    """Make the shipped behavioral oracle agree with productive helper outcomes."""
    cases = json.loads((PLUGIN_ROOT / "runtime/calibration/behavioral-cases.json").read_text(encoding="utf-8"))
    case = next(case for case in cases["cases"] if case["id"] == "model-stall-progress-without-closure")
    assert case["expected_findings"] == [
        "productive-progress-misclassified",
        "false-advisory-escalation",
        "unfinished-acceptance-must-remain-recorded",
    ]
    _runner().validate_model_stall_case_contract(cases)


@pytest.mark.integration
def test_live_guard_selftests_use_only_synthetic_current_model_policy(tmp_path: Path) -> None:
    """Plan supported models and reach CI/API guards without rewriting archived evidence."""
    runner = _runner()
    paths = runner.Paths.create("plugin", tmp_path)
    run = runner.CalibrationRun(paths)
    archived_bytes = paths.live_route_policy.read_bytes()
    live_dir = tmp_path / "guard-selftest"
    policy_path = runner.synthetic_live_guard_policy(run, live_dir)
    assert policy_path.name == "synthetic-gpt6-guard-policy.json"
    archived = json.loads(archived_bytes)
    synthetic = json.loads(policy_path.read_bytes())
    for route_id, route in synthetic["routes"].items():
        assert route["baseline_model"] == "gpt-6.1-sol"
        assert route["candidate_model"] == "gpt-6-luna"
        assert {key: value for key, value in route.items() if key not in {"baseline_model", "candidate_model"}} == {
            key: value
            for key, value in archived["routes"][route_id].items()
            if key not in {"baseline_model", "candidate_model"}
        }
    assert runner.selftest_live_ab_contract_plan(run, live_dir, policy_path)
    assert runner.selftest_live_ab_contract_paid_guards(run, live_dir, policy_path)
    assert runner.selftest_live_ab_contract_archived_policy_guard(run, live_dir)
    assert run.checks_failed == []
    checks = paths.checks.read_text(encoding="utf-8")
    assert "selftest:live-ab-synthetic-plan=64-calls:unconfirmed:no-output" in checks
    assert "selftest:live-ab-synthetic-ci-guard=blocked:no-output" in checks
    assert "selftest:live-ab-synthetic-api-key-guard=blocked:no-output" in checks
    assert "selftest:live-ab-archived-policy=blocked-before-auth:no-output" in checks
    assert paths.live_route_policy.read_bytes() == archived_bytes
    assert not any(
        (live_dir / name).exists()
        for name in ("planned-run", "ci-blocked-run", "api-key-blocked-run", "archive-blocked-run")
    )
    assert runner._live_ab_contract_fixture(run, live_dir).policy_payload == archived
