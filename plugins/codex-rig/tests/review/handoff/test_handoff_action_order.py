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

    @pytest.mark.parametrize("skill", ["implement", "investigate", "manage"])
    def test_v13_tables_preserve_actions_and_verification(self, finalizer: Any, skill: str) -> None:
        """Repeated owner/action and check fields form readable tables without mutating source records."""
        payload = _standard_payload(finalizer, skill, 13)
        payload["remaining"][0].update(
            item="Dependency is blocked.",
            owner="maintainer",
            next_action="Install dependency | rerun.\nRetain evidence.",
        )
        before = copy.deepcopy(payload)

        rendered = finalizer.render_handoff(payload)

        assert "| Owner | Issue | Action |" in rendered
        assert "| maintainer | Dependency is blocked. | Install dependency \\| rerun.<br>Retain evidence. |" in rendered
        assert "| Check | Status | Evidence |" in rendered
        assert "| tests | pass | 2 passed |" in rendered
        assert "| types |" not in rendered
        assert rendered.index("**Artifact**") < rendered.index("**Next steps**")
        assert payload == before

    def test_v13_action_and_verification_paths_stay_outside_tables(self, finalizer: Any, tmp_path: Path) -> None:
        """Action paths and existing gate logs retain their exact destinations below the respective tables."""
        payload = _standard_payload(finalizer, "implement", 13)
        payload["remaining"][0]["next_action"] = "Inspect `src/worker.py` and rerun."
        payload["verification"][3]["evidence"] = "tests.stdout.txt"
        (tmp_path / "tests.stdout.txt").write_text("2 passed\n", encoding="utf-8")

        rendered = finalizer.render_handoff(payload, report_root=tmp_path)
        table_lines = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))

        assert "src/worker.py" not in table_lines
        assert tmp_path.as_posix() not in table_lines
        assert "- External CI: src/worker.py" in rendered
        assert f"- tests: {tmp_path.as_posix()}/tests.stdout.txt" in rendered
        assert "Inspect" in rendered
        assert "and rerun." in rendered

    def test_v13_no_remaining_work_adds_no_action_table(self, finalizer: Any) -> None:
        """A successful result with no follow-up does not invent a Next steps table."""
        payload = _standard_payload(finalizer, "implement", 13)
        payload.update(remaining=[], next_steps=[])

        rendered = finalizer.render_handoff(payload)

        assert "**Next steps**" not in rendered

    def test_v13_ambiguous_issues_keep_distinct_path_anchors(self, finalizer: Any) -> None:
        """Repeated owners retain separate issue anchors when their issue text needs a detail fallback."""
        payload = _standard_payload(finalizer, "implement", 13)
        payload["tables"][0]["rows"][0]["source_ids"] = ["report:CR-2"]
        payload["tables"][0]["rows"].insert(
            0, {"id": "CR-1", "cells": ["Parser", "Fixed", "Passed", "None"], "source_ids": ["report:CR-1"]}
        )
        payload["remaining"] = [
            {
                "row_id": "CR-1",
                "owner": "author",
                "item": "`pytest` blocked",
                "next_action": "Inspect `src/one/state.py`.",
            },
            {
                "row_id": "CR-2",
                "owner": "author",
                "item": "`mypy` blocked",
                "next_action": "Inspect `src/two/state.py`.",
            },
        ]
        payload["next_steps"] = ["CR-1", "CR-2"]

        rendered = finalizer.render_handoff(payload)

        assert "- Item 1 (Issue): `pytest` blocked" in rendered
        assert "- Item 2 (Issue): `mypy` blocked" in rendered
        assert "- Item 1: src/one/state.py" in rendered
        assert "- Item 2: src/two/state.py" in rendered

    def test_v13_exact_caller_output_is_unchanged(self, finalizer: Any) -> None:
        """A caller-owned exact output bypasses every human table transformation."""
        payload = _standard_payload(finalizer, "implement", 13)
        payload.update(branch="caller-contract", tables=[], source_records=[], remaining=[], next_steps=[])
        payload["source_coverage"] = {
            "source_records_total": 0,
            "represented_source_records_total": 0,
            "omitted_source_records_total": 0,
        }
        payload["caller_contract"] = {"format": "text", "evidence": "User requested exact output", "output": "done"}

        assert finalizer.render_handoff(payload) == "done"

    def test_v12_render_matches_recorded_baseline_digest(self, finalizer: Any) -> None:
        """The new tables do not rewrite previously bound v12 reports."""
        payload = _standard_payload(finalizer, "implement", 12)

        rendered = finalizer.render_handoff(payload)

        assert hashlib.sha256(rendered.encode()).hexdigest() == (
            "46fff39540a7842d760faa34e3224403037998892f0f9eb8e825142da5802ab3"
        )

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

    @pytest.mark.parametrize("version", [12, 13])
    def test_closed_review_has_no_next_steps(self, finalizer: Any, version: int) -> None:
        """A closed v12 code review renders no Next steps block when nothing is left to do.

        The payload has no tables, source records or next steps and an empty source-coverage tally, so a follow-up
        section would be noise.
        """
        payload = handoff_payload()
        payload.update(
            skill="code-review",
            branch="closed",
            presentation_version=version,
            tables=[],
            source_records=[],
            next_steps=[],
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
