"""Hold the review truth table and the validator that enforces it to one meaning.

``skills/review/modes/verdict-states.md`` lists every valid and every impossible Gate × Outcome × Readiness state of a
version-one review header, and names who enforces each impossible one. Each row here becomes a real publication on disk
(report, validation envelope, findings rows) that goes through ``find_review_report.py`` exactly as ``/oss:resolve``
intake and the review's own publication gate read it. A valid state must pass both; an impossible state must be refused
where the table says it is, and nowhere stricter, so saved reports keep reading.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import find_review_report as frr
import pytest
from _verdict_publication import (
    AXES_LINE,
    REJECT_GATE,
    Publication,
    candidate,
    confirmed,
    judged,
    missing,
    undecided,
)

_PLUGIN = Path(__file__).resolve().parents[2]
_TABLE = _PLUGIN / "skills/review/modes/verdict-states.md"
_DEFAULT_JUDGED = "MINOR_EDITS"
_HEAD = "abcdef1234567"
_SOFTWARE_LINEUP = "sw-engineer | spawned | roles: Software engineer\n"


def _section_rows(heading: str) -> list[list[str]]:
    """Return the cells of every data row in the first table under ``heading``, read line by line."""
    lines = _TABLE.read_text(encoding="utf-8").splitlines()
    start = lines.index(heading)
    rows: list[list[str]] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] in {"State", "--", "Named coverage gap"} or set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def _slug(cell: str) -> str:
    """Strip the backticks around a state name."""
    return cell.strip("`")


def _gates(cell: str) -> list[str]:
    """Expand a Gate cell into the header values a fixture may carry."""
    if cell.startswith("REJECT_"):
        return [REJECT_GATE]
    return [f"REVIEW_V1_{word}" for word in cell.split(" or ")]


def _outcome(cell: str) -> str | None:
    """Turn an Outcome cell into the header value: ``judged`` stands for a non-reject judgment, ``null`` for none."""
    if cell == "judged":
        return _DEFAULT_JUDGED
    return None if cell == "null" else cell


def _reviewers(outcome: str | None) -> list[dict]:
    """Return one completed reviewer whose judgment supports ``outcome``."""
    return [undecided("Software engineer")] if outcome is None else [judged("Software engineer", outcome)]


Builder = Callable[[str, "str | None", str], Publication]


def _plain(gate: str, outcome: str | None, readiness: str) -> Publication:
    """Publish the three axes over one reviewer who supports the outcome, with nothing else open."""
    return Publication(gate, outcome, readiness, _reviewers(outcome))


def _hardware_blocked(gate: str, outcome: str | None, readiness: str) -> Publication:
    """Publish a judgment whose one candidate stays UNVERIFIED for an absent hardware class."""
    blocker = candidate("V1", "UNVERIFIED", missing_capability_kind="hardware")
    return Publication(gate, outcome, readiness, _reviewers(outcome), [blocker], confidence="0.80 — no CUDA host")


def _missing_reviewer(gate: str, outcome: str | None, readiness: str) -> Publication:
    """Publish beside a required reviewer that produced no output."""
    return Publication(gate, outcome, readiness, [*_reviewers(outcome), missing("Documentation reviewer")])


def _open_candidate(gate: str, outcome: str | None, readiness: str) -> Publication:
    """Publish beside a candidate left UNVERIFIED for an absent hardware class."""
    return Publication(gate, outcome, readiness, _reviewers(outcome), [candidate("V1", "UNVERIFIED")])


def _judged_only(gate: str, outcome: str | None, readiness: str) -> Publication:
    """Publish a null outcome although every completed reviewer judged the source."""
    return Publication(gate, outcome, readiness, [judged("Software engineer", _DEFAULT_JUDGED)])


def _judged_reject_gate(gate: str, outcome: str | None, readiness: str) -> Publication:
    """Publish a premise-rejection gate beside a source judgment that is not REJECT."""
    return Publication(gate, outcome, readiness, _reviewers(outcome))


def _package_blocked(gate: str, outcome: str | None, readiness: str) -> Publication:
    """Publish readiness blocked only by a package the review environment could have provisioned."""
    blocker = candidate("V1", "UNVERIFIED", missing_capability_kind="package")
    return Publication(gate, outcome, readiness, _reviewers(outcome), [blocker], confidence="0.80 — fixture")


def _approve_beside_high(gate: str, outcome: str | None, readiness: str) -> Publication:
    """Publish APPROVE beside a CONFIRMED high-severity finding."""
    record, row = confirmed("C1", "high")
    return Publication(gate, outcome, readiness, _reviewers(outcome), [record], [row])


def _unslotted_promoted(gate: str, outcome: str | None, readiness: str) -> Publication:
    """Publish full readiness although the lineup left a promoted unit without a slot."""
    return Publication(
        gate,
        outcome,
        readiness,
        _reviewers(outcome),
        lineup=_SOFTWARE_LINEUP
        + "docs+lint | promoted-unslotted: CHANGELOG.md changed | roles: Documentation reviewer\n",
        body="- Unreviewed dimension: docs+lint — CHANGELOG.md changed; rerun with --full",
    )


#: One fixture per named state. The table and this mapping must name the same states; ``test_every_state_has_a_fixture``
#: fails when either side gains a state the other lacks.
_BUILDERS: dict[str, Builder] = {
    "judged-complete": _plain,
    "judged-blocked": _hardware_blocked,
    "approve-blocked": _hardware_blocked,
    "judged-incomplete": _missing_reviewer,
    "null-blocked": _plain,
    "null-incomplete": lambda g, o, r: Publication(g, o, r, [undecided("Challenger"), missing("Linting expert")]),
    "premise-reject": _plain,
    "late-premise-reject-incomplete": _missing_reviewer,
    "late-premise-reject-blocked": _hardware_blocked,
    "null-complete": _plain,
    "reject-without-reject-gate": _plain,
    "reject-gate-without-reject": _judged_reject_gate,
    "null-without-undecidable-reviewer": _judged_only,
    "complete-with-open-work": _open_candidate,
    "blocked-with-owed-work": _missing_reviewer,
    "blocked-by-provisionable-tooling": _package_blocked,
    "approve-with-required-change": _approve_beside_high,
    "complete-with-unslotted-promoted-unit": _unslotted_promoted,
}


def _cases(heading: str) -> list:
    """Expand every row under ``heading`` into one case per Gate value it allows."""
    cases = []
    for cells in _section_rows(heading):
        slug = _slug(cells[0])
        for gate in _gates(cells[1]):
            enforced = cells[6] if len(cells) > 6 else "valid"
            label = "reject" if gate == REJECT_GATE else gate.removeprefix("REVIEW_V1_").lower()
            cases.append(pytest.param(slug, gate, _outcome(cells[2]), cells[3], enforced, id=f"{slug}-{label}"))
    return cases


def _write(tmp_path: Path, slug: str, gate: str, outcome: str | None, readiness: str) -> Path:
    """Build the fixture for one table row and write it under ``tmp_path``."""
    publication = _BUILDERS[slug](gate, outcome, readiness)
    if slug == "null-incomplete":
        publication.lineup = "challenger | spawned | roles: Challenger\n"
    elif slug != "complete-with-unslotted-promoted-unit":
        publication.lineup = _SOFTWARE_LINEUP
    (tmp_path / "head-sha.txt").write_text(_HEAD, encoding="utf-8")
    return publication.write(tmp_path)


def test_table_version_matches_validator() -> None:
    """The table states the version the validator enforces, so a changed table cannot drift from the code."""
    assert f"Truth table version: {frr.VERDICT_STATES_VERSION}" in _TABLE.read_text(encoding="utf-8").splitlines()


def test_every_state_has_a_fixture() -> None:
    """Every state the table names has a fixture here, and no fixture names a state the table dropped."""
    named = {
        _slug(cells[0]) for heading in ("## Valid states", "## Impossible states") for cells in _section_rows(heading)
    }
    assert named == set(_BUILDERS)


@pytest.mark.parametrize(("slug", "gate", "outcome", "readiness", "enforced"), _cases("## Valid states"))
def test_valid_state_passes_intake_and_producer(
    tmp_path: Path, slug: str, gate: str, outcome: str | None, readiness: str, enforced: str
) -> None:
    """A valid state reads at resolve intake and passes the review's publication gate.

    Covers the mixes that look contradictory side by side and are not: a CI ``BLOCK`` gate beside a judged outcome, and
    ``APPROVE`` beside blocked readiness (no required change, verification unfinished).
    """
    report = _write(tmp_path, slug, gate, outcome, readiness)
    line, reason = frr.gate_verdict(report)
    assert line, reason
    produced, producer_reason = frr.publication_verdict(report, producer=True)
    assert produced == line, producer_reason


@pytest.mark.parametrize(("slug", "gate", "outcome", "readiness", "enforced"), _cases("## Impossible states"))
def test_impossible_state_refused_where_the_table_says(
    tmp_path: Path, slug: str, gate: str, outcome: str | None, readiness: str, enforced: str
) -> None:
    """An impossible state is refused exactly where its ``Enforced by`` column says, and never stricter.

    ``intake`` states fail resolve intake and grade at least one ``--assess`` gap; ``producer`` states pass intake
    (saved reports keep reading) and fail only the publication gate; ``construction`` states are not validated, and a
    premise rejection keeps its reject route.
    """
    report = _write(tmp_path, slug, gate, outcome, readiness)
    line, reason = frr.gate_verdict(report)
    if enforced == "intake":
        assert not line
        assert frr.assess_report(report).gaps, reason
    elif enforced == "producer":
        assert line, reason
        produced, producer_reason = frr.publication_verdict(report, producer=True)
        assert not produced
        assert producer_reason
    else:
        assert enforced == "construction"
        assert line.startswith("Gate: REJECT_")


def test_producer_ceilings_match_validator() -> None:
    """The Readiness ceilings the table marks ``producer`` are exactly the ones the validator enforces."""
    rows = _section_rows("## Confidence ceilings")
    stated = {
        cells[0].split("`")[1]: float(cells[1]) for cells in rows if cells[2] == "producer" and "Readiness" in cells[0]
    }
    assert stated == dict(frr.READINESS_CONFIDENCE_CEILINGS)


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("skills/review/templates/review-report.md", id="report-template"),
        pytest.param("skills/review/templates/consolidator-prompt.md", id="consolidator"),
    ],
)
def test_axes_line_is_written_verbatim(relative: str) -> None:
    """The report template and the consolidator carry the same fixed line naming Gate as the CI merge gate.

    The header key stays ``Gate:`` because the delivery hook, the protocol and resolve intake read it by name; the
    meaning travels in this line instead, so ``Gate: REVIEW_V1_PASS`` beside a revision outcome stops reading as a
    contradiction.
    """
    assert AXES_LINE in (_PLUGIN / relative).read_text(encoding="utf-8")


def test_terminal_publisher_writes_the_axes_line() -> None:
    """The acceptance gate's terminal publisher, which bypasses the consolidator, writes the same line."""
    text = (_PLUGIN / "skills/review/modes/acceptance-gate.md").read_text(encoding="utf-8")
    assert any("the fixed `Axes:` line from `templates/review-report.md`" in line for line in text.splitlines())


def test_capability_kinds_match_consolidator() -> None:
    """The consolidator offers exactly the missing-capability kinds the validator accepts."""
    text = (_PLUGIN / "skills/review/templates/consolidator-prompt.md").read_text(encoding="utf-8")
    rule = next(line for line in text.splitlines() if "carries `missing_capability_kind`" in line)
    for kind in frr.MISSING_CAPABILITY_KINDS:
        assert f"`{kind}` (" in rule


@pytest.mark.parametrize(
    ("fill_gaps", "passes"),
    [
        pytest.param(True, True, id="fill-gaps-carried-verbatim"),
        pytest.param(False, False, id="fresh-run-record"),
    ],
)
def test_carried_candidate_without_kind_passes_the_producer(tmp_path: Path, fill_gaps: bool, passes: bool) -> None:
    """A blocked candidate that predates the kind key passes the publication gate only as a fill-gaps carry.

    A fill-gaps run copies carried candidates verbatim and its carry check refuses any added key, so the gate must not
    demand the key on a record the consolidator is forbidden to change; the consolidator rule says so too. A fresh
    publication omitting the key would evade the provisionable-tooling rule, so it is refused.
    """
    report = _hardware_blocked("REVIEW_V1_PASS", _DEFAULT_JUDGED, "blocked")
    report.candidates = [{k: v for k, v in candidate("V1", "UNVERIFIED").items() if k != "missing_capability_kind"}]
    report.lineup = None if fill_gaps else _SOFTWARE_LINEUP
    path = report.write(tmp_path)
    (tmp_path / "head-sha.txt").write_text(_HEAD, encoding="utf-8")
    plan = {"head_sha": _HEAD, "carried": {"candidates": ["V1"]}}
    (tmp_path / "fill-gaps.json").write_text(json.dumps(plan), encoding="utf-8")
    line, reason = frr.publication_verdict(path, producer=True, fill_gaps=fill_gaps)
    assert (line == "Gate: BLOCK") is passes, reason
    text = (_PLUGIN / "skills/review/templates/consolidator-prompt.md").read_text(encoding="utf-8")
    rule = next(line for line in text.splitlines() if "carries `missing_capability_kind`" in line)
    assert "A fill-gaps run copies carried candidates verbatim and never adds these keys to one" in rule
