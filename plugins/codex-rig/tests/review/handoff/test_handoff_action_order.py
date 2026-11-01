"""Verify final action placement and historical handoff compatibility."""

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest
from _final_handoff_support import handoff_payload

FINALIZER = Path(__file__).resolve().parents[3] / "shared" / "final_handoff.py"


def _standard_payload(finalizer: Any, skill: str, version: int) -> dict:
    """Build one standard result whose single table row carries an explicitly owned outstanding action."""
    payload = handoff_payload()
    payload.update(skill=skill, presentation_version=version)
    payload["tables"] = [
        {
            "heading": "Results",
            "columns": list(finalizer.STANDARD_COLUMNS[skill]),
            "rows": [
                {
                    "id": "CR-2",
                    "cells": ["Parser", "Fixed", "Passed", "External CI"],
                    "source_ids": ["report:CR-1", "report:CR-2"],
                }
            ],
        }
    ]
    return payload


class TestHandoffActionOrder:
    """Where the Next steps block sits relative to the Artifact block across presentation versions."""

    def test_v12_unresolved_action_is_last_and_complete(self, finalizer: Any) -> None:
        """Presentation v12 ends the handoff with the unresolved item's action and leaves the payload untouched.

        A standard implement result carries one outstanding item owned by a maintainer. The v12 renderer must put the
        Next steps block after the Artifact block, finish on that item's action line, and not mutate its input.
        """
        payload = _standard_payload(finalizer, "implement", 12)
        payload["remaining"][0].update(
            item="Dependency is blocked.", owner="maintainer", next_action="Install the dependency and rerun the check."
        )
        before = copy.deepcopy(payload)

        rendered = finalizer.render_handoff(payload)

        assert rendered.index("**Artifact**") < rendered.index("**Next steps**")
        assert rendered.endswith("   Action: Install the dependency and rerun the check.\n")
        assert "1. **Owner: maintainer**\n\n   Status: Dependency is blocked." in rendered
        assert payload == before

    def test_v12_closed_review_has_no_next_steps(self, finalizer: Any) -> None:
        """A closed v12 code review renders no Next steps block when nothing is left to do.

        The payload has no tables, source records or next steps and an empty source-coverage tally, so a follow-up
        section would be noise.
        """
        payload = handoff_payload()
        payload.update(
            skill="code-review", branch="closed", presentation_version=12, tables=[], source_records=[], next_steps=[]
        )
        payload["source_coverage"] = {
            "source_records_total": 0,
            "represented_source_records_total": 0,
            "omitted_source_records_total": 0,
        }
        payload["outcome"] = {"title": "Review closed", "summary": "The review is closed."}

        rendered = finalizer.render_handoff(payload)

        assert "**Next steps**" not in rendered

    @pytest.mark.parametrize("version", [8, 9, 10, 11])
    def test_historical_versions_keep_next_steps_before_artifact(self, finalizer: Any, version: int) -> None:
        """Presentation v8 to v11 still place Next steps before Artifact.

        Older recorded handoffs must re-render in their original order; only v12 moved the action to the end.
        """
        payload = _standard_payload(finalizer, "implement", version)

        rendered = finalizer.render_handoff(payload)

        assert rendered.index("**Next steps**") < rendered.index("**Artifact**")

    def test_v11_render_matches_recorded_baseline_digest(self, finalizer: Any) -> None:
        """The v11 standard implement handoff renders byte for byte as the recorded baseline.

        The digest pins the historical rendering, so any accidental change to v11 output shows up as a hash mismatch.
        """
        payload = _standard_payload(finalizer, "implement", 11)

        rendered = finalizer.render_handoff(payload)

        assert (
            hashlib.sha256(rendered.encode()).hexdigest()
            == "247f07601173080ddd14cff1b40883b6ce05fa0cf8de89b60ab739540ff51e54"
        )
