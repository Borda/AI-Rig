"""Regression checks for renaming the live analysis skill to assess."""

from __future__ import annotations

import json
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ASSESS_SKILL = PLUGIN_ROOT / "skills" / "assess" / "SKILL.md"
ASSESS_TEMPLATE = ASSESS_SKILL.with_name("result-template.json")
OLD_SKILL = PLUGIN_ROOT / "skills" / "change-analysis" / "SKILL.md"
PACKAGE_MANIFEST = PLUGIN_ROOT / "package-manifest.json"


def test_assess_replaces_change_analysis_as_the_discoverable_skill() -> None:
    """Prevent a renamed skill from leaving a live old-name discovery path."""
    skill = ASSESS_SKILL.read_text(encoding="utf-8")
    template = json.loads(ASSESS_TEMPLATE.read_text(encoding="utf-8"))

    assert "name: assess" in skill
    assert '"approve_gh": "optional boolean; default false' in skill
    assert "ASSESS_METADATA" in skill
    assert template["artifact_path"] == ".reports/codex/assess/<timestamp>/result.json"
    assert (
        template["metadata"]["final_handoff"]["handoff_path"] == ".reports/codex/assess/<timestamp>/final-handoff.json"
    )
    assert not OLD_SKILL.exists()


def test_regenerated_package_manifest_exposes_assess_not_change_analysis() -> None:
    """Prevent packaging the retired skill after its source directory is gone."""
    manifest = json.loads(PACKAGE_MANIFEST.read_text(encoding="utf-8"))
    skill_ids = {skill["id"] for skill in manifest["skills"]}

    assert "assess" in skill_ids
    assert "change-analysis" not in skill_ids
