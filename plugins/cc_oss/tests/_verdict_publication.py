"""Build complete version-one review publications for the verdict-state, confidence and fan-out contract tests.

Each publication is the trio ``find_review_report.py`` reads (``review-report.md``, ``review-validation.json``,
``findings.jsonl``) plus, when asked, the Step 2 ``lineup.md`` record. The builders take the three header axes as
arguments, so a test can drive them straight from the rows of ``skills/review/modes/verdict-states.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from _review_fixtures import concern, finding_row
from _text_io import write_lf

#: The exact line every version-one report carries after the rating legend, naming what each header axis means.
AXES_LINE = (
    "Axes: Gate = CI merge gate at the reviewed head, or a premise rejection · Outcome = source judgment · "
    "Readiness = verification state; each axis is read on its own."
)
#: Current rating legend, written right after the header as the consolidator does.
LEGEND_LINE = "Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block."
#: Header gate value of a premise rejection; the reviewed commit is a fixed fake id.
REJECT_GATE = "REVIEW_V1_REJECT_SCOPE @abcdef1234567"
_RATINGS = {"APPROVE": 1, "MINOR_EDITS": 2, "MAJOR_REVISION": 3, "INSUFFICIENT_BUT_PROMISING": 4, "REJECT": 5}


def judged(role: str, outcome: str, readiness: str = "complete") -> dict:
    """Describe one completed reviewer who judged the source with ``outcome``."""
    return {
        "role": role,
        "presence": "completed",
        "source_outcome": outcome,
        "source_rating": _RATINGS[outcome],
        "readiness": readiness,
        "coverage": "src/",
        "rationale": "inspected the changed source",
        "evidence": ["src/a.py:1"],
        "limits": [],
    }


def undecided(role: str) -> dict:
    """Describe one completed reviewer who inspected the source but could not judge it (null, readiness blocked)."""
    return {**judged(role, "APPROVE", "blocked"), "source_outcome": None, "source_rating": None}


def missing(role: str) -> dict:
    """Describe one required reviewer that produced no output (``presence: missing``, readiness incomplete)."""
    return {
        "role": role,
        "presence": "missing",
        "source_outcome": None,
        "source_rating": None,
        "readiness": "incomplete",
        "coverage": "",
        "rationale": "",
        "evidence": [],
        "limits": ["no output"],
    }


def candidate(identifier: str, disposition: str | None, **extra: object) -> dict:
    """Describe one evidence-bound candidate with the given disposition; ``extra`` adds or overrides keys."""
    record = {
        "id": identifier,
        "claim": "value dropped on the native path",
        "revision": "abcdef1234567",
        "contract": "all input values preserved",
        "trigger": "two values on the native path",
        "counterevidence": [],
        "limits": [],
        "checks": [
            {
                "method": "source inspection",
                "result": "control flow traced",
                "revision": "abcdef1234567",
                "environment": "CPU host",
                "limits": [],
            }
        ],
        "disposition": disposition,
        "reason": "traced at the reviewed head",
        "feasible_checks_complete": disposition is not None,
    }
    if disposition == "UNVERIFIED":
        record["missing_capability"] = "native accelerator"
        record["missing_capability_kind"] = "hardware"
    record.update(extra)
    return record


#: Lineup record of a fresh run whose one ranked unit spawned: the publication gate requires a lineup outside fill-gaps.
SPAWNED_LINEUP = "sw-engineer | spawned\n"


@dataclass
class Publication:
    """One review publication to write: header axes plus the envelope records and the lineup record.

    ``lineup`` defaults to a fresh run's record (one spawned unit); ``None`` writes no ``lineup.md``, as fill-gaps does.
    """

    gate: str
    outcome: str | None
    readiness: str
    assessments: list[dict]
    candidates: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    confidence: str = "0.70 — fixture"
    lineup: str | None = SPAWNED_LINEUP
    body: str = ""

    def write(self, directory: Path) -> Path:
        """Write the publication into ``directory`` and return the report path."""
        report = directory / "review-report.md"
        header = (
            f"---\nTitle: oss-review — fixture\nPR: #7\nReview contract: 1\nRating scale: source-5-v2\n"
            f"Readiness: {self.readiness}\nGate: {self.gate}\nOutcome: {self.outcome or 'null'}\n"
            f"Summary: Intended behavior: yes. Fixture.\nConfidence: {self.confidence}\n---\n"
        )
        write_lf(report, f"{header}\n{LEGEND_LINE}\n\n{AXES_LINE}\n\n{self.body}\n")
        open_ids = [c["id"] for c in self.candidates if c.get("disposition") in (None, "UNVERIFIED")]
        envelope = {
            "contract_version": 1,
            "rating_scale": "source-5-v2",
            "source_outcome": self.outcome,
            "source_rating": _RATINGS.get(self.outcome) if self.outcome else None,
            "readiness": self.readiness,
            "readiness_reason": "fixture readiness basis",
            "assessments": self.assessments,
            "candidates": self.candidates,
            "open_concerns": [concern(cid) for cid in open_ids],
        }
        write_lf(directory / "review-validation.json", json.dumps(envelope))
        write_lf(directory / "findings.jsonl", "".join(json.dumps(row) + "\n" for row in self.findings))
        if self.lineup is not None:
            write_lf(directory / "lineup.md", self.lineup)
        return report


def confirmed(identifier: str, severity: str) -> tuple[dict, dict]:
    """Return a CONFIRMED candidate and its merge-ready findings row at ``severity``."""
    return candidate(identifier, "CONFIRMED"), {**finding_row(identifier), "severity": severity}
