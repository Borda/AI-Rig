"""A remediation whose bounded implementation challenge could not run still finalizes and promotes as complete.

The challenge is a limitation, never a completion gate: with every selected finding closed and every gate passing, the
real finalizer promotes a passing result that names ``implementation challenge not run: <reason>`` among its limits and
carries the ``implementation-challenge-not-run`` confidence gap, with no unresolved row, remaining work or next step.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_remediation_finalize import HELPER, VALIDATOR, _draft_handoff, _stale_metadata


def _record_unrun_challenge(result: dict[str, object], reason: str) -> None:
    """Replace the fixture's confidence explanation with the unrun challenge limitation and its named gap."""
    gap = f"(-0.05) implementation-challenge-not-run: {reason}."
    metadata = result["metadata"]
    metadata["confidence_gaps"] = [gap]
    metadata["confidence_gap_closures"] = [
        {"gap": gap, "status": "unresolved", "rationale": "No independent route ran; no independent coverage claimed."}
    ]
    metadata["confidence_recovery"]["remaining_limits"] = [f"implementation challenge not run: {reason}."]


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param("approval review denied the bridge call", id="approval-denied"),
        pytest.param("no independent route was available", id="route-unavailable"),
        pytest.param("the user declined the pass", id="user-declined"),
    ],
)
def test_unrun_challenge_promotes_a_complete_remediation(
    valid_run: tuple[Path, dict[str, object]],
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
    reason: str,
) -> None:
    """Closed findings plus passing gates complete the run; the unrun challenge stays a named limit only."""
    run, result = valid_run
    _record_unrun_challenge(result, reason)
    drafts = tmp_path_factory.mktemp("drafts")
    (drafts / "metadata.json").write_text(json.dumps(_stale_metadata(result)), encoding="utf-8")
    (drafts / "handoff.json").write_text(json.dumps(_draft_handoff(run)), encoding="utf-8")
    argv = ["finalize", "--run", str(run), "--metadata", str(drafts / "metadata.json")]
    argv += ["--handoff", str(drafts / "handoff.json"), "--status", "pass"]
    argv += ["--confidence", "0.95", "--artifact-path", result["artifact_path"], "--promote"]

    exit_code = HELPER.main(argv)

    summary = json.loads(capsys.readouterr().out)
    assert (exit_code, summary["status"], summary["errors"], summary["promoted"]) == (0, "pass", [], True)
    VALIDATOR.validate("code-remediate", run, run / "result.json")
    promoted = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert promoted["status"] == "pass"
    assert promoted["metadata"]["unresolved_summary"]["unresolved_reason_groups"] == []
    assert promoted["metadata"]["unresolved_summary"]["selected_items_unresolved"] == 0
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    assert (handoff["remaining"], handoff["next_steps"]) == ([], [])
    assert handoff["confidence"]["limits"] == [f"implementation challenge not run: {reason}."]
    final = (run / "final.md").read_text(encoding="utf-8")
    assert f"implementation challenge not run: {reason}" in final
    assert f"implementation-challenge-not-run: {reason}" in final
