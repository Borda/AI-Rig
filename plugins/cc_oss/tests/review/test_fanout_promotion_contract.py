"""Hold the review's fan-out cap to disclosing what it leaves unreviewed.

The default run spawns at most ``FANOUT_MAX`` ranked units. A unit whose territory holds a changed changelog or
migration guide, a CI workflow file or a stated performance claim is promoted into a slot ahead of the others; a
promoted unit that still finds no slot is a missing required reviewer, and every unit that did not run is named in the
report. These tests read the launch and consolidation instructions line by line, and run the publication gate on a
fixture PR that touches a changelog, a Python file and a performance claim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import find_review_report as frr
import pytest
from _text_io import write_lf
from _verdict_publication import Publication, judged, missing

_PLUGIN = Path(__file__).resolve().parents[2]
_LAUNCH = _PLUGIN / "skills/review/modes/agent-launch.md"
_SKILL = _PLUGIN / "skills/review/SKILL.md"
_PROMPTS = _PLUGIN / "skills/review/templates/agent-prompts.md"
_CONSOLIDATOR = _PLUGIN / "skills/review/templates/consolidator-prompt.md"
_ATTRIBUTION_MARKER = "**Reviewer attribution (additive):**"


def _lines(path: Path) -> list[str]:
    """Return the lines of one skill file."""
    return path.read_text(encoding="utf-8").splitlines()


def _promotion_rule() -> str:
    """Return the promotion bullet nested under the relevance-ranking stage of the spawn-count gate."""
    return next(line for line in _lines(_LAUNCH) if line.strip().startswith("- **Promotion — inside the cap"))


def _ranked_units() -> set[str]:
    """Return the spawn units the skill lists as rankable under ``FANOUT_MAX``."""
    lines = _lines(_SKILL)
    start = next(i for i, line in enumerate(lines) if line.startswith("FANOUT_MAX="))
    comment = []
    for line in lines[start + 1 :]:
        text = line.strip().lstrip("#").strip()
        if text.startswith("OUTSIDE"):
            break
        comment.append(text)
    listed = re.sub(r"\([^)]*\)", "", " ".join(comment).split("units:", 1)[1])
    return {unit.strip() for unit in listed.split("·") if unit.strip()}


def _units_spawned_only_in_ci_modes() -> set[str]:
    """Return the units whose agent row says they spawn only in the CI/CD-only and docs+CI/CD modes."""
    only_ci = set()
    for line in _lines(_PROMPTS):
        match = re.match(r"\*\*Agent \d+ — (?:\w+:)?([a-z\-]+) \(CI/CD-only mode and docs\+CI/CD mode\)", line)
        if match:
            only_ci.add(match.group(1))
    return only_ci


class TestLaunchRule:
    """The launch step promotes trigger-holding units inside the cap and records its lineup."""

    @pytest.mark.parametrize(
        "trigger",
        [
            pytest.param("a changed `CHANGELOG`/`CHANGES`/`HISTORY`/`NEWS` file", id="changelog"),
            pytest.param("a changed migration or upgrade guide", id="migration-doc"),
            pytest.param("sw-engineer ← a changed GitHub Actions file (`.github/workflows/`", id="ci-workflow"),
            pytest.param("a stated performance claim", id="performance-claim"),
        ],
    )
    def test_promotion_triggers(self, trigger: str) -> None:
        """Each trigger the audit named promotes the unit whose territory holds it."""
        assert trigger in _promotion_rule()

    def test_every_trigger_names_a_unit_that_exists_where_it_fires(self) -> None:
        """A promotion goes to a unit of the code lineup, never to one that spawns only in the CI-only modes.

        ``oss:cicd-steward`` is the sole unit of those modes, so a trigger promoting it would fire in a mixed code and
        workflow PR, where it is not spawned, and assign the workflow change to no reviewer.
        """
        rule = _promotion_rule()
        triggers = rule.split("Triggers:", 1)[1].split("`oss:cicd-steward` is never promoted", 1)[0]
        receivers = set(re.findall(r"(?:^|; )\s*([a-z+\-]+)(?: \([^)]*\))? ←", triggers))
        assert receivers == {"docs+lint", "sw-engineer", "perf+arch"}
        ci_only = _units_spawned_only_in_ci_modes()
        assert ci_only == {"cicd-steward"}
        assert receivers <= _ranked_units() - ci_only
        assert "`oss:cicd-steward` is never promoted" in rule

    def test_promotion_never_raises_the_cap(self) -> None:
        """The cap and ``--full`` stay the cost decision: promotion reorders slots, it never adds one."""
        assert "`FANOUT_MAX` and `--full` stay the cost decision: promotion never adds a slot." in _promotion_rule()

    def test_unslotted_promoted_unit_is_missing_coverage(self) -> None:
        """A promoted unit left without a slot lowers Readiness instead of disappearing from the report."""
        rule = next(
            line for line in _lines(_LAUNCH) if line.strip().startswith("- A promoted unit left without a slot")
        )
        assert "`presence: missing`" in rule
        assert "Readiness is `incomplete`" in rule

    def test_lineup_record_is_written(self) -> None:
        """The launch turn writes one lineup line per preselected unit, in the states the gate reads."""
        rule = next(line for line in _lines(_LAUNCH) if "`$REPORT_DIR/lineup.md`" in line)
        assert "`<unit> | spawned | roles: <role>, <role>`" in rule
        assert "`<unit> | spawned | promoted: <trigger> | roles: <role>, <role>`" in rule
        assert "`<unit> | promoted-unslotted: <trigger> | roles: <role>, <role>`" in rule
        assert "`<unit> | dropped`" in rule
        assert "including each role covered by a merged spawn" in rule


class TestConsolidatorDisclosure:
    """The consolidator names every unit that did not run, outside the parity-pinned attribution list."""

    def test_attribution_list_keeps_its_wording(self) -> None:
        """The attribution list still omits skipped roles; the disclosure rule lives outside it, so parity holds."""
        lines = _lines(_CONSOLIDATOR)
        start = lines.index(next(line for line in lines if line.startswith(_ATTRIBUTION_MARKER)))
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("- "):
                block.append(line)
            elif block:
                break
        assert any("omit execution-origin labels and skipped roles" in line for line in block)
        assert not any("lineup.md" in line for line in block)

    @pytest.mark.parametrize(
        "statement",
        [
            pytest.param("to exactly one `review-validation.json` assessment", id="one-assessment-per-spawned-role"),
            pytest.param("Each `promoted-unslotted` unit is a missing required reviewer", id="unslotted-missing"),
            pytest.param("`Unreviewed dimension: <unit> — dropped by FANOUT_MAX`", id="dropped-named"),
            pytest.param(
                "Readiness is unchanged and the dropped-unit confidence ceiling applies", id="dropped-ceiling"
            ),
        ],
    )
    def test_disclosure_rules(self, statement: str) -> None:
        """Dropped units are named with their ceiling; unslotted promoted units become missing reviewers."""
        lines = _lines(_CONSOLIDATOR)
        start = next(i for i, line in enumerate(lines) if line.startswith("**Coverage reconciliation"))
        section = []
        for line in lines[start + 1 :]:
            if line.startswith("## "):
                break
            section.append(line)
        assert any(statement in line for line in section)


#: The fixture PR: it changes ``CHANGELOG.md`` and a Python file and its body claims a speed-up, so docs+lint and
#: perf+arch both hold promotion triggers. The lineup lines below are what the launch step records for each outcome.
_SOFTWARE = "sw-engineer | spawned | roles: Software engineer\n"
_DOCS = "docs+lint | spawned | promoted: CHANGELOG.md changed | roles: Documentation reviewer\n"
_PERF = "perf+arch | spawned | promoted: 2x speed-up claimed | roles: Performance engineer\n"
_SPAWNED = _SOFTWARE + _DOCS + _PERF
_UNSLOTTED = (
    _SOFTWARE + _PERF + "docs+lint | promoted-unslotted: CHANGELOG.md changed | roles: Documentation reviewer\n"
)
_DROPPED = _SOFTWARE + _DOCS + "challenger | dropped\n"
_DOCS_LINE = (
    "- Unreviewed dimension: docs+lint — CHANGELOG.md changed. Rerun with `/oss:review 7 --full` or "
    '`/oss:review 7 --fill-gaps=.reports/review/pr-7/run-001/review-report.md --roles="Documentation reviewer"`.'
)
_CHALLENGER_LINE = "- Unreviewed dimension: challenger — dropped by FANOUT_MAX. Rerun with `/oss:review 7 --full`."
_HEAD = "a" * 40


def _publish(tmp_path: Path, lineup: str, readiness: str, body: str, *, missing_docs: bool = False) -> Path:
    """Write one publication of the fixture PR with the given lineup record, Readiness and report body."""
    run = tmp_path / ".reports" / "review" / "pr-7" / "run-001"
    run.mkdir(parents=True)
    write_lf(run / "head-sha.txt", _HEAD + "\n")
    (run / "review-publication.published").touch()
    reviewers = [
        judged("Software engineer", "MINOR_EDITS"),
        judged("Performance engineer", "MINOR_EDITS"),
        missing("Documentation reviewer") if missing_docs else judged("Documentation reviewer", "MINOR_EDITS"),
    ]
    return Publication(
        "REVIEW_V1_PASS", "MINOR_EDITS", readiness, reviewers, lineup=lineup, body=f"### Review Confidence\n\n{body}"
    ).write(run)


@pytest.mark.parametrize(
    ("lineup", "readiness", "body", "missing_docs", "refusal"),
    [
        pytest.param(_SPAWNED, "complete", "", False, "", id="promoted-unit-spawned"),
        pytest.param(
            _UNSLOTTED,
            "complete",
            _DOCS_LINE,
            False,
            "is promoted-unslotted, so readiness must be incomplete",
            id="unslotted-claims-complete",
        ),
        pytest.param(
            _UNSLOTTED,
            "incomplete",
            "",
            True,
            "generated `Unreviewed dimension: docs+lint` line",
            id="unslotted-not-named",
        ),
        pytest.param(_UNSLOTTED, "incomplete", _DOCS_LINE, True, "", id="unslotted-disclosed"),
        pytest.param(
            _DROPPED, "complete", "", False, "generated `Unreviewed dimension: challenger` line", id="dropped-not-named"
        ),
        pytest.param(_DROPPED, "complete", _CHALLENGER_LINE, False, "", id="dropped-named"),
    ],
)
def test_capped_run_either_spawns_or_discloses(
    tmp_path: Path, lineup: str, readiness: str, body: str, missing_docs: bool, refusal: str
) -> None:
    """The publication gate passes a run that spawned the promoted unit or disclosed it, and refuses one that hid it.

    Resolve intake never reads the lineup record: every case passes intake, so the rule binds fresh publications only.
    """
    report = _publish(tmp_path, lineup, readiness, body, missing_docs=missing_docs)
    intake, reason = frr.gate_verdict(report)
    assert intake, reason
    line, producer_reason = frr.publication_verdict(report, producer=True)
    if refusal:
        assert not line
        assert refusal in producer_reason
    else:
        assert line == intake, producer_reason


@pytest.mark.parametrize(
    ("fill_gaps", "passes"),
    [
        pytest.param(False, False, id="fresh-run-must-write-its-lineup"),
        pytest.param(True, True, id="fill-gaps-run-writes-none"),
    ],
)
def test_publication_without_lineup_record(tmp_path: Path, fill_gaps: bool, passes: bool) -> None:
    """Only a fill-gaps run, named by ``--fill-gaps`` beside its plan, publishes without a lineup record.

    A fresh ranked run whose lineup Write was skipped would otherwise ship a capped lineup with nothing disclosed.
    Resolve intake still reads such a report: saved reports predate the record.
    """
    report = Publication(
        "REVIEW_V1_PASS", "MINOR_EDITS", "complete", [judged("Software engineer", "MINOR_EDITS")], lineup=None
    ).write(tmp_path)
    write_lf(tmp_path / "head-sha.txt", _HEAD + "\n")
    write_lf(tmp_path / "fill-gaps.json", json.dumps({"head_sha": _HEAD, "carried": {"candidates": []}}))
    line, reason = frr.publication_verdict(report, producer=True, fill_gaps=fill_gaps)
    assert (line == "Gate: PASS", frr.gate_verdict(report)[0]) == (passes, "Gate: PASS"), reason
