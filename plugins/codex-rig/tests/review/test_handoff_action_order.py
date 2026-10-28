"""Verify final action placement and historical handoff compatibility."""

import copy
import hashlib

import pytest
from test_final_handoff import _handoff_payload, _load_finalizer
from test_readable_handoff import _standard_payload


def test_v12_unresolved_actions_are_last_and_complete():
    payload = _standard_payload("implement", 12)
    payload["remaining"][0].update(
        item="Dependency is blocked.", owner="maintainer", next_action="Install the dependency and rerun the check."
    )
    before = copy.deepcopy(payload)
    rendered = _load_finalizer().render_handoff(payload)
    assert rendered.index("**Artifact**") < rendered.index("**Next steps**")
    assert rendered.endswith("   Action: Install the dependency and rerun the check.\n")
    assert "1. **Owner: maintainer**\n\n   Status: Dependency is blocked." in rendered
    assert payload == before


def test_v12_closed_has_no_follow_up():
    payload = _handoff_payload()
    payload.update(
        skill="code-review", branch="closed", presentation_version=12, tables=[], source_records=[], next_steps=[]
    )
    payload["source_coverage"] = {
        "source_records_total": 0,
        "represented_source_records_total": 0,
        "omitted_source_records_total": 0,
    }
    payload["outcome"] = {"title": "Review closed", "summary": "The review is closed."}
    rendered = _load_finalizer().render_handoff(payload)
    assert "**Next steps**" not in rendered


@pytest.mark.parametrize("version", [8, 9, 10, 11])
def test_history_retains_bytes(version):
    renderer = _load_finalizer()
    payload = _standard_payload("implement", version)
    rendered = renderer.render_handoff(payload)
    assert rendered.index("**Next steps**") < rendered.index("**Artifact**")


def test_v11_retains_recorded_baseline_bytes():
    rendered = _load_finalizer().render_handoff(_standard_payload("implement", 11))
    assert (
        hashlib.sha256(rendered.encode()).hexdigest()
        == "247f07601173080ddd14cff1b40883b6ce05fa0cf8de89b60ab739540ff51e54"
    )
