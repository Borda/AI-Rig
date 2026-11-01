"""Review-validation record builders shared by the review fill-gaps, resolve gap-triage and validation contract tests.

Each builder returns one JSON-shaped record of the review validation envelope or the actionable ``findings.jsonl`` rows,
so the three suites assemble the same shapes the shipped scripts read.
"""

from __future__ import annotations


def assessment(role: str, readiness: str = "complete", *, outcome: str = "NEEDS_WORK", rating: int = 3) -> dict:
    """Describe one completed reviewer with the given readiness and judgment (NEEDS_WORK, rating 3 by default)."""
    return {
        "role": role,
        "presence": "completed",
        "source_outcome": outcome,
        "source_rating": rating,
        "readiness": readiness,
        "coverage": "src/",
        "rationale": "inspected",
        "evidence": ["src/a.py:1"],
        "limits": [],
    }


def concern(candidate_id: str, closure: str = "assert passes") -> dict:
    """Carry one open candidate as a durable concern."""
    return {"candidate_id": candidate_id, "owner": "maintainer", "next_check": "native run", "closure": closure}


def finding_row(candidate_id: str) -> dict:
    """Publish one confirmed candidate as an actionable row carrying every field resolve's merge requires."""
    return {
        "id": f"f-{candidate_id}",
        "candidate_id": candidate_id,
        "contract_version": 1,
        "validation_status": "CONFIRMED",
        "section": "Architecture & Quality",
        "severity": "medium",
        "title": "backend drops value",
        "change": "code",
        "author": "QA",
    }


def without(row: dict, *keys: str) -> dict:
    """Copy a findings row without the given fields."""
    return {key: value for key, value in row.items() if key not in keys}
