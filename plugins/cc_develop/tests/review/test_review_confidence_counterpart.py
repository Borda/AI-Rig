"""Pin the confidence, verdict and coverage instructions shipped with local review."""

from pathlib import Path

import pytest

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"


@pytest.fixture
def consolidator_lines() -> list[str]:
    """Read the shipped consolidator prompt one line at a time."""
    return (_REVIEW / "templates/consolidator-prompt.md").read_text(encoding="utf-8").splitlines()


def test_codex_confidence_comes_from_the_bridge_envelope(consolidator_lines: list[str]) -> None:
    """Only a completed bridge envelope with numeric confidence contributes a Codex score."""
    assert all("fixed confidence 0.75" not in line for line in consolidator_lines)
    rule = next(line for line in consolidator_lines if line.startswith("- Codex confidence"))
    assert "only the numeric `confidence` field from the bridge envelope" in rule
    assert "`status: complete`" in rule
    assert "Null, blocked, timed-out, or absent" in rule
    assert "exclude them from min, median and aggregate" in rule
    assert "**Gaps**" in rule
    assert "observed cause" in rule


def test_provisionable_inputs_and_host_os_stay_out_of_gaps(consolidator_lines: list[str]) -> None:
    """Provisionable packages have a closing check, and hardware limits identify the exact class."""
    package = next(line for line in consolidator_lines if line.startswith("- A missing or out-of-spec Python package"))
    assert "never a Confidence gap" in package
    assert "provisionable in a local ephemeral environment" in package
    assert "name the targeted check that would close it under `Next steps`" in package
    assert any("host OS is never a Confidence gap" in line for line in consolidator_lines)
    hardware = next(line for line in consolidator_lines if line.startswith("- Absent hardware"))
    assert "Name exactly the absent hardware class" in hardware
    assert "nothing broader" in hardware


def test_verdict_stays_independent_of_gate_and_readiness(consolidator_lines: list[str]) -> None:
    """The report explains the axes and reserves approval for cases without confirmed major defects."""
    axes = next(line for line in consolidator_lines if "`Axes:" in line)
    assert "Gate = CI merge gate" in axes
    assert "Outcome = source judgment" in axes
    assert "Readiness = verification state" in axes
    assert axes.split("`")[1] in (_REVIEW / "templates/review-report.md").read_text(encoding="utf-8")
    approval = next(line for line in consolidator_lines if "`APPROVE` never stands beside" in line)
    assert "CONFIRMED `critical` or `high`" in approval
    assert "`APPROVE` beside `blocked` or `incomplete` readiness is valid" in approval


@pytest.mark.parametrize(
    ("condition", "ceiling"),
    [
        pytest.param("Readiness `incomplete`", "0.70", id="incomplete-readiness"),
        pytest.param("Readiness `blocked`", "0.80", id="blocked-readiness"),
        pytest.param("Reviewed code not executed", "0.80", id="no-runtime-evidence"),
        pytest.param("A unit dropped by the cap or left unslotted after promotion", "0.85", id="omitted-unit"),
    ],
)
def test_confidence_ceiling_matches_coverage(consolidator_lines: list[str], condition: str, ceiling: str) -> None:
    """Each coverage condition carries its own ceiling in the inline table."""
    row = next(line for line in consolidator_lines if line.startswith(f"| {condition}"))
    assert row.split("|")[2].strip() == ceiling


def test_promotion_reorders_slots_and_discloses_omitted_units(consolidator_lines: list[str]) -> None:
    """Rank relevant triggers inside the existing cap and name all dropped and unslotted units."""
    lines = (_REVIEW / "SKILL.md").read_text(encoding="utf-8").splitlines()
    ranking = next(i for i, line in enumerate(lines) if line.startswith("3. **Relevance ranking**"))
    promotion, unslotted = (line.strip() for line in lines[ranking + 1 : ranking + 3])
    assert promotion.startswith("- **Promotion within the cap.**")
    assert "changelog or migration guide" in promotion
    assert "CI workflow" in promotion
    assert "performance claim" in promotion
    assert "ahead of lower-evidence units" in promotion
    assert "`Unreviewed dimension: <unit>" in unslotted
    assert "0.85" in unslotted
    defaults = [line.split("#", 1)[0].strip() for line in lines if line.startswith("FANOUT_CAP=")]
    assert defaults == ["FANOUT_CAP=3"]
    disclosure = next(line for line in consolidator_lines if line.startswith("**Coverage disclosure.**"))
    assert "one `Unreviewed dimension: <unit>" in disclosure
    assert "every dropped or unslotted unit" in disclosure


def test_report_template_exposes_the_fields_the_consolidator_requires(consolidator_lines: list[str]) -> None:
    """The template carries the Codex row, the coverage disclosure line and the capped Aggregate the rules write."""
    template = (_REVIEW / "templates/review-report.md").read_text(encoding="utf-8").splitlines()
    row = next(line for line in template if line.startswith("| Codex co-reviewer |"))
    assert [cell.strip() for cell in row.strip("|").split("|")][1] == "—"
    assert any(line.startswith("- Unreviewed dimension: [unit] —") for line in template)
    aggregate = next(line for line in template if line.startswith("**Aggregate**:"))
    assert aggregate.startswith("**Aggregate**: min 0.N / median 0.N / ceiling 0.N (<binding coverage gap>)")
    assert aggregate.endswith("→ 0.N")
    rule = next(line for line in consolidator_lines if line.startswith("**Confidence ceilings.**"))
    assert "capped by the lowest applicable ceiling" in rule
    assert "both the `**Aggregate**` score" in rule


def test_template_uses_the_consolidators_codex_scoring_rule() -> None:
    """A single shared rule decides whether an envelope or a derived partition score enters the aggregate."""
    lines = (_REVIEW / "templates/review-report.md").read_text(encoding="utf-8").splitlines()
    row = next(line for line in lines if line.startswith("| Codex co-reviewer |"))
    numeric = next(line for line in lines if line.startswith("> Numeric scores only"))
    assert all("[Codex lane scoring]" in line for line in (row, numeric))
    assert any("derived multi-wave score" in line for line in (row, numeric))
    assert all("only when status is complete" not in line for line in (row, numeric))


@pytest.mark.parametrize(
    ("label", "required"),
    [
        pytest.param("Cap-dropped", "cap reason", id="ordinary-cap-omission"),
        pytest.param("Promoted but unslotted", "required roles", id="missing-required-coverage"),
        pytest.param("No omissions", "No cap omissions", id="complete-lineup"),
    ],
)
def test_lineup_records_cap_decisions_before_dispatch(label: str, required: str) -> None:
    """The parent distinguishes ordinary omissions, missing required coverage and a complete lineup."""
    lines = (_REVIEW / "SKILL.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## Step 3:"))
    end = next(i for i, line in enumerate(lines) if line.startswith("### Merged spawn units"))
    lineup = lines[start:end]
    assert any("`$RUN_DIR/lineup.md`" in line and "Write tool" in line for line in lineup)
    assert any(required in line for line in lineup if line.startswith(f"- **{label}**"))


def test_validation_reads_the_durable_coverage_records() -> None:
    """An unavailable record stays visible as incomplete coverage instead of yielding a complete report."""
    lines = (_REVIEW / "SKILL.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## Step 4:"))
    end = lines.index("## Step 5: Consolidate findings")
    step = lines[start:end]
    assert any("Read `$RUN_DIR/lineup.md` and `$RUN_DIR/codex-review-reply.md`" in line for line in step)
    assert any("coverage evidence is incomplete" in line for line in step)
    assert any("from those records" in line and "cross-validation.md" in line for line in step)
