"""Verify final action placement and historical handoff compatibility."""

import copy
import hashlib
import json
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

    def test_owned_action_is_the_only_recommendation_among_closed_rows(self, finalizer: Any) -> None:
        """An outstanding prerequisite gives one concrete way forward while a completed row adds no action."""
        payload = _standard_payload(finalizer, "investigate", 15)
        payload["action_policy"] = "owned"
        rows = payload["tables"][0]["rows"]
        rows[0].update(cells=["Parser", "Receipt", "Confirmed", "Inspect CI."], source_ids=["report:CR-2"])
        rows.insert(0, {"id": "CR-1", "cells": ["Copier", "Receipt", "Closed", "None"], "source_ids": ["report:CR-1"]})
        payload["remaining"][0].update(owner="maintainer", next_action="Rerun CI once the worker is available.")

        actions = finalizer.render_handoff(payload).split("**Recommendations / next steps**\n\n")[1]

        assert actions.splitlines() == [
            "| Owner | Issue | Action |",
            "| -- | -- | -- |",
            "| maintainer | External CI | Rerun CI once the worker is available. |",
        ]

    def test_complete_diagnosis_with_no_action_cells_has_no_recommendation_rows(self, finalizer: Any) -> None:
        """A closed hypothesis does not acquire a made-up owner or a placeholder next step."""
        payload = _standard_payload(finalizer, "investigate", 15)
        payload["action_policy"] = "owned"
        payload["tables"][0]["rows"][0]["cells"] = ["Parser", "Verified", "Closed", "—"]
        payload.update(remaining=[], next_steps=[])

        assert finalizer.render_handoff(payload).endswith(
            "**Recommendations / next steps**\n\nNo further action is required.\n"
        )

    def test_best_way_forward_keeps_waiting_work_and_closes_with_one_route(self, finalizer: Any) -> None:
        """Two waiting items stay accounted for while a single dependency-unblocking choice closes the report."""
        payload = _standard_payload(finalizer, "implement", 15)
        payload["action_policy"] = "best-way-forward"
        payload["tables"][0]["rows"][0]["source_ids"] = ["report:CR-2"]
        payload["tables"][0]["rows"].append(
            {"id": "CR-1", "cells": ["Parser", "Blocked", "Pending", "CI"], "source_ids": ["report:CR-1"]}
        )
        payload["remaining"].append(
            {"row_id": "CR-1", "owner": "maintainer", "item": "Parser awaits CI", "next_action": "Inspect CI results."}
        )

        rendered = finalizer.render_handoff(payload)
        actions = rendered.split("**Recommendations / next steps**\n\n")[1]

        assert "| maintainer | Parser awaits CI |" in rendered
        assert actions.splitlines()[2:] == ["| ci | External CI | Rerun CI. |"]
        payload["next_steps"].append("CR-1")
        with pytest.raises(finalizer.HandoffError, match="best-way-forward-requires-one-step"):
            finalizer.render_handoff(payload)

    def test_actionable_result_row_requires_owned_follow_up(self, finalizer: Any) -> None:
        """A genuine but unbound repair proposal must survive as an explicitly owned recommendation."""
        payload = _standard_payload(finalizer, "investigate", 15)
        payload["action_policy"] = "owned"
        payload["tables"][0]["rows"][0]["cells"] = ["Parser", "Verified", "Confirmed", "Correct the receipt."]
        payload.update(remaining=[], next_steps=[])

        with pytest.raises(finalizer.HandoffError, match="table-action-requires-remaining:CR-2"):
            finalizer.render_handoff(payload)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("next_action", "None", id="empty-action"),
            pytest.param("next_action", "No change.", id="completed-status"),
            pytest.param("owner", "—", id="absent-owner"),
            pytest.param("owner", "Unassigned", id="unassigned-owner"),
        ],
    )
    def test_recommendation_requires_real_owner_and_action(self, finalizer: Any, field: str, value: str) -> None:
        """A reserved absence marker cannot stand in for the requested owner or next action."""
        payload = _standard_payload(finalizer, "implement", 15)
        payload["action_policy"] = "owned"
        payload["remaining"][0][field] = value

        with pytest.raises(finalizer.HandoffError, match=f"remaining-{field}-placeholder:CR-2"):
            finalizer.render_handoff(payload)

    def test_current_diagnosis_keeps_judgment_and_recommendations(self, finalizer: Any) -> None:
        """A completed diagnosis still delivers its repair recommendations without empty outcome labels."""
        payload = _standard_payload(finalizer, "investigate", 15)
        payload["outcome"] = {"title": "Local validation", "summary": "Two defects confirmed; repairs are required."}
        payload.update(remaining=[], next_steps=[])
        payload["tables"][0]["rows"][0]["cells"] = [
            "Receipt mismatch",
            "Producer/consumer disagreement.",
            "Confirmed in installed 2.3.4; review/remediation is affected.",
            "Maintainer: align producer/consumer receipts.",
        ]
        original = copy.deepcopy(payload)

        rendered = finalizer.render_handoff(payload)

        assert rendered.startswith("Two defects confirmed; repairs are required.\n")
        assert "Local validation" not in rendered
        assert "See detail" not in rendered
        assert "| Hypothesis | Evidence | Disposition |" in rendered
        assert "Confirmed in installed 2.3.4; review/remediation is affected." in rendered
        assert rendered.index("**Artifact**") < rendered.index("**Recommendations / next steps**")
        assert rendered.count("Maintainer: align producer/consumer receipts.") == 1
        assert rendered.rstrip().endswith("| — | Receipt mismatch | Maintainer: align producer/consumer receipts. |")
        assert payload == original

    def test_current_actions_merge_owned_and_table_recommendations(self, finalizer: Any) -> None:
        """An owned action supersedes its display cell without hiding other rows' recommendations."""
        payload = _standard_payload(finalizer, "investigate", 15)
        payload["tables"][0]["rows"][0].update(
            cells=["Parser", "Evidence", "Confirmed", "Older action"], source_ids=["report:CR-2"]
        )
        payload["tables"][0]["rows"].append(
            {
                "id": "CR-1",
                "cells": ["Copier", "Evidence", "Confirmed", "Narrow copied inputs."],
                "source_ids": ["report:CR-1"],
            }
        )

        rendered = finalizer.render_handoff(payload)
        actions = rendered.split("**Recommendations / next steps**\n\n", 1)[1]

        assert "Older action" not in rendered
        assert "| ci | External CI | Rerun CI. |" in actions
        assert "| — | Copier | Narrow copied inputs. |" in actions

    def test_current_completed_work_has_explicit_final_action_status(self, finalizer: Any) -> None:
        """A clean implementation ends with an explicit no-follow-up judgment."""
        payload = _standard_payload(finalizer, "implement", 15)
        payload.update(remaining=[], next_steps=[])

        rendered = finalizer.render_handoff(payload)

        assert rendered.endswith("**Recommendations / next steps**\n\nNo further action is required.\n")

    def test_current_action_paths_are_externalized_without_hiding_prose(self, finalizer: Any) -> None:
        """Explicit source references leave the table while commands and slash prose remain readable."""
        payload = _standard_payload(finalizer, "implement", 15)
        payload["remaining"][0].update(
            item="Review/remediation receipts", next_action="Inspect `src/worker.py` with `pytest` and rerun."
        )

        rendered = finalizer.render_handoff(payload)
        actions = rendered.split("**Recommendations / next steps**\n\n", 1)[1]

        assert "See detail" not in actions
        assert "| ci | Review/remediation receipts | Inspect worker.py with `pytest` and rerun. |" in actions
        assert "- Review/remediation receipts: src/worker.py" in actions

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("pytest tests/unit", id="directory-argument"),
            pytest.param("python shared/check.py --strict", id="script-with-option"),
            pytest.param("python shared/check.py", id="script-argument"),
            pytest.param('os.path.join("src/file")', id="call-with-path-string"),
            pytest.param("len(src/path)", id="expression-with-slash"),
        ],
    )
    def test_current_inline_commands_remain_complete(self, finalizer: Any, command: str) -> None:
        """A slash in a command argument never turns the complete command into a shortened path."""
        payload = _standard_payload(finalizer, "implement", 15)
        payload["remaining"][0]["next_action"] = f"Run `{command}` and inspect `./source files/worker.py`."

        rendered = finalizer.render_handoff(payload)
        actions = rendered.split("**Recommendations / next steps**\n\n", 1)[1]

        assert f"Run `{command}` and inspect worker.py." in actions
        assert "- External CI: ./source files/worker.py" in actions
        assert f"- External CI: {command}" not in actions

    def test_prior_diagnosis_bytes_remain_unchanged(self, finalizer: Any) -> None:
        """Previously bound reports retain their original title, action placement and prose fallback."""
        payload = _standard_payload(finalizer, "investigate", 14)
        payload["outcome"] = {"title": "Local validation", "summary": "Two defects confirmed; repairs are required."}
        payload.update(remaining=[], next_steps=[])
        payload["tables"][0]["rows"][0]["cells"] = [
            "Receipt mismatch",
            "Producer/consumer disagreement.",
            "Confirmed in installed 2.3.4; review/remediation is affected.",
            "Maintainer: align producer/consumer receipts.",
        ]

        rendered = finalizer.render_handoff(payload)

        assert hashlib.sha256(rendered.encode()).hexdigest() == (
            "c96b1944e90c98154236a8006cb9e57bb25e4a7a8955bb6c5016b9d63d2ff4ee"
        )

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

    def test_v13_verification_links_stay_in_table_and_action_paths_stay_outside(
        self, finalizer: Any, tmp_path: Path
    ) -> None:
        """Verification logs stay clickable in their rows while action paths retain their external list."""
        payload = _standard_payload(finalizer, "implement", 13)
        payload["remaining"][0]["next_action"] = "Inspect `src/worker.py` and rerun."
        payload["verification"][3]["evidence"] = "tests.stdout.txt"
        (tmp_path / "tests.stdout.txt").write_text("2 passed\n", encoding="utf-8")

        rendered = finalizer.render_handoff(payload, report_root=tmp_path)
        table_lines = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))

        assert "src/worker.py" not in table_lines
        assert "| Check | Status | Evidence |" in table_lines
        assert f"| tests | pass | [Log](<{tmp_path.as_posix()}/tests.stdout.txt>) |" in table_lines
        assert "- External CI: src/worker.py" in rendered
        assert "- tests:" not in rendered
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

    @pytest.mark.parametrize("version", [13, 15])
    def test_v13_exact_caller_output_is_unchanged(self, finalizer: Any, version: int) -> None:
        """A caller-owned exact output bypasses every human table transformation."""
        payload = _standard_payload(finalizer, "implement", version)
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

    @pytest.mark.parametrize("version", [12, 13, 15])
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
        assert "**Recommendations / next steps**" not in rendered

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

    def test_new_ordinary_handoff_defaults_to_owned_actions_before_binding(
        self, finalizer: Any, tmp_path: Path
    ) -> None:
        """Direct rendering enforces the same authorship contract as the automated finalizer."""
        payload = _standard_payload(finalizer, "investigate", 15)
        handoff = tmp_path / "final-handoff.json"
        final = tmp_path / "final.md"
        validation = tmp_path / "final-handoff.validation.json"
        handoff.write_text(json.dumps(payload), encoding="utf-8")

        assert finalizer.render_files(handoff, final, validation) == finalizer.check_files(handoff, final, validation)
        assert json.loads(handoff.read_bytes()) == {**payload, "action_policy": "owned"}

    def test_invalid_unowned_action_does_not_overwrite_fresh_draft(self, finalizer: Any, tmp_path: Path) -> None:
        """A policy default never changes the source or writes output when an authored action lacks an owner."""
        payload = _standard_payload(finalizer, "investigate", 15)
        payload.update(remaining=[], next_steps=[])
        payload["tables"][0]["rows"][0]["cells"] = ["Parser", "Receipt", "Confirmed", "Fix the receipt."]
        handoff = tmp_path / "final-handoff.json"
        original = json.dumps(payload).encode()
        handoff.write_bytes(original)
        final, validation = tmp_path / "final.md", tmp_path / "final-handoff.validation.json"

        with pytest.raises(finalizer.HandoffError, match="table-action-requires-remaining:CR-2"):
            finalizer.render_files(handoff, final, validation)
        assert handoff.read_bytes() == original
        assert not final.exists()
        assert not validation.exists()

    def test_reissuing_bound_historical_handoff_preserves_its_exact_bytes(self, finalizer: Any, tmp_path: Path) -> None:
        """Retained proof protects the original payload and presentation when an unmarked report is rendered again."""
        payload = _standard_payload(finalizer, "investigate", 15)
        payload.update(remaining=[], next_steps=[])
        payload["tables"][0]["rows"][0]["cells"] = ["Parser", "Receipt", "Closed", "None"]
        handoff, final, validation = (
            tmp_path / "final-handoff.json",
            tmp_path / "final.md",
            tmp_path / "final-handoff.validation.json",
        )
        source = json.dumps(payload).encode()
        rendered = finalizer.render_handoff(payload).encode()
        proof = {
            "schema_version": 1,
            "status": "pass",
            "skill": "investigate",
            "branch": "standard",
            "handoff_sha256": hashlib.sha256(source).hexdigest(),
            "rendered_sha256": hashlib.sha256(rendered).hexdigest(),
        }
        receipt = json.dumps(proof).encode()
        handoff.write_bytes(source)
        final.write_bytes(rendered)
        validation.write_bytes(receipt)

        assert finalizer.render_files(handoff, final, validation) == proof
        assert (handoff.read_bytes(), final.read_bytes(), validation.read_bytes()) == (source, rendered, receipt)
