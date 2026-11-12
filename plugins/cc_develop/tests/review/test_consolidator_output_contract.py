"""Guard the local review's severity, attribution and compact summary instructions."""

import json
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parents[2] / "skills/review/templates"


def test_intent_floor_preserves_medium_scope_findings() -> None:
    """Only contract contradictions and implementation mismatches receive the intent HIGH floor."""
    lines = (_TEMPLATES / "consolidator-prompt.md").read_text(encoding="utf-8").splitlines()
    intent = next(line for line in lines if line.startswith("Intended behavior (always):"))
    assert "`intent: contradicts`" in intent
    assert "`intent: undeclared/unimplemented behavior`" in intent
    assert "`intent: outside project scope` remains MEDIUM" in intent


def test_summary_keeps_intent_basis_in_body() -> None:
    """A bounded header summary retains the verdict while the body retains its full evidence basis."""
    lines = (_TEMPLATES / "consolidator-prompt.md").read_text(encoding="utf-8").splitlines()
    intent = next(line for line in lines if line.startswith("Intended behavior (always):"))
    summary = next(line for line in lines if line.startswith("| `Summary:` |"))
    assert "100 characters" in summary
    assert "1–2 sentence" not in summary.split(";", 1)[0]
    assert "1–2 sentence overview belong in the body" in summary
    assert "full `Intended behavior: <verdict> — <basis>`" in intent
    assert "body's opening aggregate review summary" in intent


def test_root_cause_merge_retains_local_records_without_oss_consumer() -> None:
    """Local deduplication retains validated symptoms without imposing another plugin's resolution taxonomy."""
    lines = (_TEMPLATES / "consolidator-prompt.md").read_text(encoding="utf-8").splitlines()
    merge = next(line for line in lines if line.startswith("- Root-cause merge:"))
    assert "merged_into" in merge
    assert "never discards a confirmed candidate" in merge
    assert "Resolve" not in merge
    assert "GitHub" not in merge
    assert "/oss:resolve" not in merge


def test_design_divergence_compares_local_change() -> None:
    """A local diff review compares the change without assuming a pull request exists."""
    lines = (_TEMPLATES / "consolidator-prompt.md").read_text(encoding="utf-8").splitlines()
    divergence = next(line for line in lines if line.startswith("- Add `### Design Divergence`"))
    assert "from the change's approach" in divergence


def test_display_budget_uses_defined_severity_counts() -> None:
    """The display target uses the same severity count as pre-flight without undefined shorthand."""
    lines = (_TEMPLATES / "review-checklist.md").read_text(encoding="utf-8").splitlines()
    target = next(line for line in lines if line.startswith("- Target:"))
    assert "number of CRITICAL/HIGH findings plus 2" in target
    assert "GT" not in target


def test_reviewer_assessments_reach_consolidation() -> None:
    """Every dimension supplies the scoped assessment fields consumed by consolidation."""
    skill = (_TEMPLATES.parent / "SKILL.md").read_text(encoding="utf-8").splitlines()
    handoff = next(line for line in skill if line.startswith('> "Write your FULL findings'))
    assert "source_outcome" in handoff
    assert "source_rating" in handoff
    assert "rationale" in handoff
    assert "readiness_reason" in handoff
    assert "inspected coverage" in handoff


def test_compact_reviewer_envelope_retains_judgment_basis() -> None:
    """The returned envelope carries rationale and readiness reasons alongside its scoped ratings."""
    lines = (_TEMPLATES.parent / "SKILL.md").read_text(encoding="utf-8").splitlines()
    handoff = next(line for line in lines if line.startswith('> "Write your FULL findings'))
    example = handoff.split("compact JSON envelope", 1)[1].split("`", 2)[1]
    envelope = json.loads(example.replace('\\"', '"').replace('"findings":N', '"findings":0'))
    assert {
        "source_outcome",
        "source_rating",
        "rationale",
        "readiness",
        "readiness_reason",
        "coverage",
    } <= envelope.keys()
    assert envelope["rationale"]
    assert envelope["readiness_reason"]


def test_header_order_follows_report_template() -> None:
    """Attribution follows readiness consistently in the table, template and reviewer instruction."""
    lines = (_TEMPLATES / "consolidator-prompt.md").read_text(encoding="utf-8").splitlines()
    table = [line.split("|")[1].strip() for line in lines if line.startswith("| `")]
    assert table.index("`Agents:`") < table.index("`Review contract:` / `Rating scale:`")
    assert table.index("`Readiness:`") < table.index("`Reviewers:`") < table.index("`CI:`")
    attribution = next(line for line in lines if line.startswith("- Add `Reviewers:`"))
    assert "immediately after `Readiness:`" in attribution


def test_ci_reporting_preserves_actual_evidence_and_static_check_boundary() -> None:
    """No fixed CI result overrides actual evidence or forbids allowed non-mutating checks."""
    lines = (_TEMPLATES / "consolidator-prompt.md").read_text(encoding="utf-8").splitlines()
    ci = next(line for line in lines if line.startswith("| `CI:`"))
    assert "Actual CI evidence supplied by the parent" in ci
    assert "static analysis as a test run" in ci
    assert any("installed static analyzers" in line and "without fixes" in line for line in lines)
    assert not any("develop:review executes no code" in line for line in lines)
