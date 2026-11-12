"""Protect QA scope and architect decision delivery in specialist spawn instructions."""

from pathlib import Path

import pytest

_AGENTS = Path(__file__).resolve().parents[2] / "agents"


def _section(agent: str, start: str, end: str) -> list[str]:
    """Return one section as lines so checks do not depend on prose wrapping."""
    source = (_AGENTS / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    lines = []
    for line in source:
        lines.append(line)
        marker = f'cat "${{CLAUDE_PLUGIN_ROOT}}/references/{agent}/'
        if marker in line:
            relative = line.split('cat "${CLAUDE_PLUGIN_ROOT}/', 1)[1].split('"', 1)[0]
            lines.extend((_AGENTS.parent / relative).read_text(encoding="utf-8").splitlines())
    first = next(index for index, line in enumerate(lines) if line.startswith(start))
    last = next(index for index in range(first + 1, len(lines)) if lines[index].startswith(end))
    return lines[first:last]


def test_qa_description_keeps_coverage_reviews_in_scope() -> None:
    """Do not route read-only coverage audits away from their QA owner."""
    lines = _section("qa-specialist", "description:", "tools:")
    assert any("SKIP: reading existing test results only" in line for line in lines)
    assert not any("SKIP: read-only;" in line for line in lines)


def test_qa_writes_tests_against_a_finalized_contract() -> None:
    """Keep implementation-time TDD with the engineer while allowing QA tests first."""
    lines = _section("qa-specialist", "<core-principles>", "</core-principles>")
    contract = next(line for line in lines if "New tests target the finalized contract" in line)
    assert "caller hands over" in contract
    assert "spec, reproduction brief, existing behavior to characterize" in contract
    assert "sw-engineer" in contract
    assert "implementation-time TDD stays with" in contract
    assert not any("New features: follow TDD" in line for line in lines)


def test_qa_baseline_precedes_test_edits() -> None:
    """Establish the same test scope before edits instead of guessing pre-existing failures."""
    lines = _section("qa-specialist", "<workflow>", "</workflow>")
    baseline = next(index for index, line in enumerate(lines) if "before editing tests" in line)
    writing = next(index for index, line in enumerate(lines) if line.startswith("07."))
    assert baseline < writing
    assert "same scope" in lines[baseline]
    assert "record" in lines[baseline]
    assert any("git worktree add --detach <tmp> <base>" in line for line in lines)
    assert any("rootdir" in line and "baseline" in line for line in lines)


def test_qa_broken_code_checks_use_a_scratch_copy() -> None:
    """Negative controls must not mutate production code in the shared invoking tree."""
    lines = _section("qa-specialist", "08.", "09.")
    assert any("scratch copy" in line and "never mutate" in line for line in lines)
    assert any("import" in line and "original" in line for line in lines)


def test_qa_caller_scope_and_budget_override_full_suite_retries() -> None:
    """Respect targeted or null plans and deliver results before the caller's turn cap."""
    lines = _section("qa-specialist", "12.", "13.")
    assert any("caller-named test scope" in line and "only" in line for line in lines)
    assert any("null" in line and "no targeted tests" in line for line in lines)
    assert any("full suite only when no scope" in line for line in lines)
    assert any("caller" in line and "budget" in line and "envelope" in line for line in lines)


def test_qa_nonexecuting_scope_skips_baseline_mutation_and_final_runs() -> None:
    """Apply caller execution restrictions to every run, including negative controls."""
    lines = _section("qa-specialist", "<workflow>", "</workflow>")
    gate = next(line for line in lines if "**Execution gate" in line)
    assert "Steps 03, 08 and 12" in gate
    assert "review-only" in gate
    assert "null" in gate
    assert "skip all test commands and mutations" in gate
    assert any(line.startswith("08.") and "Execution gate" in line for line in lines)
    assert any(line.startswith("12.") and "Execution gate" in line for line in lines)


@pytest.mark.parametrize("agent", ["qa-specialist", "solution-architect"])
def test_python_runner_does_not_bootstrap_unmanaged_projects(agent: str) -> None:
    """Avoid creating uv environments when the caller already owns the interpreter."""
    lines = _section(
        agent,
        "<workflow>" if agent == "qa-specialist" else "## API Surface Audit",
        "</workflow>" if agent == "qa-specialist" else "## Dependency Direction",
    )
    assert any("uv.lock" in line and "uv run" in line and "only" in line for line in lines)
    assert any("existing" in line and "interpreter" in line for line in lines)


def test_qa_contract_questions_do_not_require_an_interactive_tool() -> None:
    """Send ambiguous emerging conventions to the caller without blocking the subagent."""
    lines = _section("qa-specialist", "## Verify Before Asserting", "</code-review-assertions>")
    assert any("Emerging" in line and "Contract questions:" in line for line in lines)
    assert not any("prisma." in line or "`fetch`" in line for line in lines)


def test_qa_team_protocol_falls_back_to_the_running_install() -> None:
    """Read the shipped protocol without resolving a different installed plugin version."""
    lines = _section("qa-specialist", "<teammate-mode>", "</teammate-mode>")
    protocol = next(line for line in lines if "Follow AgentSpeak" in line)
    assert "cat ~/.claude/TEAM_PROTOCOL.md" in protocol
    assert 'If absent, run `cat "${CLAUDE_PLUGIN_ROOT}/TEAM_PROTOCOL.md"`' in protocol
    assert "ask the orchestrator" in protocol
    assert not any("resolve_plugin_root.py" in line for line in lines)
    assert not any("ls -td" in line for line in lines)


def test_qa_dead_code_boundary_is_explicit() -> None:
    """Route dead-code analysis consistently with the antipattern exclusion."""
    lines = _section("qa-specialist", "<routing-boundaries>", "</routing-boundaries>")
    assert any("NOT for dead-code analysis" in line for line in lines)


@pytest.mark.parametrize("agent", ["qa-specialist", "solution-architect"])
def test_declines_deliver_the_callers_file_and_response_contract(agent: str) -> None:
    """Deliver unsupported results in mandated files, envelopes or fixed closing lines."""
    lines = _section(agent, "<notes>", "</notes>")
    delivery = next(line for line in lines if "Decline delivery:" in line)
    assert "first write" in delivery
    assert "status: unsupported" in delivery
    assert '"status": "unsupported"' in delivery
    assert '"file"' in delivery
    assert "fixed closing line" in delivery
    assert "takes precedence" in delivery
    assert "no Confidence block" in delivery
    assert not any("appropriate infrastructure or security agent" in line for line in lines)


def test_architect_alignment_pauses_only_for_a_real_open_decision() -> None:
    """Allow untagged research mapping to proceed unless it actually raises a concern."""
    lines = _section("solution-architect", "03.", "04.")
    assert any("wait only" in line and "concern" in line for line in lines)
    assert any("review-only asks" in line and "misalignments as findings" in line for line in lines)
    assert any("without confirmation" in line and "untagged" in line for line in lines)
    assert any("needs_decision: true" in line for line in lines)
    assert any("Never proceed past" in line and "unanswered" in line for line in lines)
    assert any("not an authorization" in line for line in lines)


def test_architect_adr_numbering_preserves_existing_decisions() -> None:
    """Choose the next ADR number and revise an existing decision only when requested."""
    lines = _section("solution-architect", "06.", "07.")
    numbering = next(line for line in lines if "For a new ADR" in line)
    assert "docs/adr/*.md" in numbering
    assert "leading number" in numbering
    assert "ADR-" in numbering
    assert "max + 1" in numbering
    assert "start at 1 if none" in numbering
    assert "naming pattern" in numbering
    assert "padding convention" in numbering
    assert "destination is absent" in numbering
    assert any("never overwrite" in line for line in lines)
    assert any("explicitly" in line and "revise" in line for line in lines)


def test_architect_review_output_is_an_explicit_artifact_exception() -> None:
    """Permit severity-tagged review findings without forcing a new standalone ADR."""
    lines = _section("solution-architect", "<output-format>", "</output-format>")
    assert any("except review-only asks" in line and "Step 6" in line for line in lines)


def test_architect_infrastructure_does_not_route_to_python_ci_owner() -> None:
    """Restrict CI handoff to Python GitHub Actions and acknowledge unowned infrastructure."""
    lines = _section("solution-architect", "<notes>", "</notes>")
    assert any("Infrastructure/K8s" in line and "no roster owner" in line for line in lines)
    assert any("Python" in line and "GitHub Actions" in line and "oss:cicd-steward" in line for line in lines)
    assert not any("Infrastructure/K8s → `oss:cicd-steward`" in line for line in lines)


@pytest.mark.parametrize(
    ("agent", "start", "end", "filename"),
    [
        pytest.param("qa-specialist", "<pytest-config>", "</pytest-config>", "pytest-config.md", id="pytest-setup"),
        pytest.param(
            "solution-architect",
            "<design-artifacts>",
            "</design-artifacts>",
            "design-artifacts.md",
            id="design-templates",
        ),
    ],
)
def test_shared_context_loading_names_an_executable_read(agent: str, start: str, end: str, filename: str) -> None:
    """Load local shared instructions explicitly through the available Bash tool."""
    lines = _section(agent, start, end)
    assert any(filename in line and "cat" in line and "Bash" in line for line in lines)


def test_qa_declines_only_foreign_domain_requests() -> None:
    """Keep caller-handed reproduction and characterization requests out of the decline path."""
    lines = _section("qa-specialist", "<notes>", "</notes>")
    boundary = next(line for line in lines if "**Scope boundary**" in line)
    assert "Only NOT-for entries" in boundary
    assert "another domain" in boundary
    assert "non-Python frameworks/infra, mutation testing, dead-code, linting, implementation" in boundary
    assert "Caller-handed reproduction briefs and characterization requests stay in scope" in boundary
    assert "SKIP entries do not trigger this decline" in boundary
    assert "Out-of-scope inputs under" not in boundary
    assert "stop with zero findings" in boundary


@pytest.mark.parametrize("trigger", ["Return ONLY", "an envelope", "a mandated file"])
def test_architect_output_contract_identifies_subagent_context_without_tags(trigger: str) -> None:
    """Envelope and file callers must receive decisions even when interactive tools are exposed."""
    lines = _section("solution-architect", "03.", "04.")
    context = next(line for line in lines if "**Subagent context**" in line)
    assert trigger in context
    assert "regardless of tag" in context
    assert "even if `AskUserQuestion` is available" in context
    interactive = next(line for line in lines if "call `AskUserQuestion` if available" in line)
    assert "outside subagent context" in interactive
    branches = _section("solution-architect", "02.", "03.")
    assert any("in subagent context" in line and "Step 3" in line for line in branches)


def test_architect_open_decision_cannot_return_a_success_status() -> None:
    """Make the concern-only result distinguishable from a completed design artifact."""
    lines = _section("solution-architect", "03.", "04.")
    context = next(line for line in lines if "**Subagent context**" in line)
    assert '"status":"needs_decision"' in context
    assert "needs_decision: true" in context
    assert "never the caller's success status" in context
    assert "stop before Step 4" in context
    assert "write the concern and question to any mandated output file" in context
    assert "fixed closing line" in context
    assert "normal envelope" in context


@pytest.mark.parametrize("ask", ["analysis", "mapping"])
def test_architect_requested_conflicts_are_findings_instead_of_decision_gates(ask: str) -> None:
    """Return conflicts and gaps as requested analysis without demanding approval for each one."""
    lines = _section("solution-architect", "03.", "04.")
    alignment = lines[0]
    assert ask in alignment
    assert "requested output already includes conflicts or gaps" in alignment
    assert "wait only when the design question remains ambiguous" in alignment
    assert "misalignments as findings" in alignment
    concern = next(line for line in lines if "**If request appears misaligned**" in line)
    assert "analysis/mapping asks" in concern
    assert "report the concern as a finding and continue" in concern


def test_qa_parametrization_threshold_matches_the_testing_rule() -> None:
    """Avoid flagging a pair of similar tests when the shared threshold is three."""
    lines = _section("qa-specialist", "<core-principles>", "</core-principles>")
    principle = next(line for line in lines if "**Parametrization**" in line)
    assert "3+ test functions" in principle
    assert "two test functions" not in principle


def test_qa_mocking_flags_external_overmocking() -> None:
    """Do not permit external mocks to replace the behavior the test is meant to exercise."""
    lines = _section("qa-specialist", "<core-principles>", "</core-principles>")
    principle = next(line for line in lines if "**Mocking discipline**" in line)
    assert "over-mocked externals" in principle
    assert "no longer verifies real behavior" in principle


def test_qa_implementation_detail_flags_include_primary_call_order_assertions() -> None:
    """Catch tests whose primary oracle asserts call sequencing instead of the public contract."""
    lines = _section("qa-specialist", "<antipatterns-to-flag>", "</antipatterns-to-flag>")
    assert any("implementation details" in line and "call order as primary" in line for line in lines)


def test_qa_test_layout_keeps_io_and_system_tests_separate() -> None:
    """Preserve the definitions needed to distinguish unit, integration, system and smoke tests."""
    lines = _section("qa-specialist", "## Test Organization", "</core-principles>")
    assert any("tests/unit/" in line and "no I/O" in line for line in lines)
    assert any("tests/integration/" in line and "real deps/I/O" in line for line in lines)
    assert any("tests/e2e/" in line and "full system" in line for line in lines)
    assert any("tests/smoke/" in line and "deploy sanity" in line for line in lines)


def test_qa_baseline_worktree_is_session_scoped_and_cleaned_up() -> None:
    """Keep baseline fallbacks outside the invoking tree and remove their Git registrations."""
    lines = _section("qa-specialist", "12.", "13.")
    fallback = next(line for line in lines if "git worktree add --detach" in line)
    assert "outside the repository" in fallback
    assert "${TMPDIR:-/tmp}/qa-baseline-${CSID}" in fallback
    assert "git worktree remove --force <tmp>" in fallback
    assert "even if the baseline command fails" in fallback


def test_qa_testing_rule_uses_the_user_install_before_the_plugin_fallback() -> None:
    """Avoid a failing project-relative read for a rule installed in the user's home directory."""
    lines = _section("qa-specialist", "## Parametrized Tests", "## Error Path Testing")
    loading = next(line for line in lines if "Test Structure" in line)
    assert "cat ~/.claude/rules/foundry-python-testing.md" in loading
    assert 'if absent, run `cat "${CLAUDE_PLUGIN_ROOT}/rules/python-testing.md"`' in loading
    assert "cat .claude/rules/" not in loading


def test_qa_hypothesis_invariants_exclude_undocumented_nonfinite_values() -> None:
    """Avoid NaN and infinity breaking invariants outside the documented input domain."""
    lines = _section("qa-specialist", "## Hypothesis for Data Transformations", "</property-based-testing>")
    assert any("exclude NaN/inf unless documented" in line for line in lines)
