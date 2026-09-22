"""Verify the agreed GPT-6 model and effort assignments in active source routing."""

import json
from pathlib import Path
import re


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ROLES = REPOSITORY_ROOT / "plugins" / "codex-rig" / "roles"
EXPECTED = {
    "challenger": ("gpt-6-sol", "high"),
    "cicd-steward": ("gpt-6-luna", "high"),
    "curator": ("gpt-6-luna", "high"),
    "data-steward": ("gpt-6-sol", "high"),
    "delegation-lead": ("gpt-6-luna", "high"),
    "doc-scribe": ("gpt-6-luna", "high"),
    "linting-expert": ("gpt-6-luna", "medium"),
    "oss-shepherd": ("gpt-6-luna", "high"),
    "qa-specialist": ("gpt-6-sol", "medium"),
    "scientist": ("gpt-6-sol", "high"),
    "security-auditor": ("gpt-6-sol", "high"),
    "solution-architect": ("gpt-6-sol", "high"),
    "squeezer": ("gpt-6-sol", "medium"),
    "sw-engineer": ("gpt-6-sol", "medium"),
    "web-explorer": ("gpt-6-luna", "medium"),
}


def test_gpt6_role_model_and_effort_map() -> None:
    """Keep each specialist on its separately selected capability and effort axes."""
    assert {path.parent.name for path in ROLES.glob("*/ROLE.md")} == set(EXPECTED)
    for role, (model, effort) in EXPECTED.items():
        card = (ROLES / role / "ROLE.md").read_text(encoding="utf-8")
        assert re.search(rf"^model: {re.escape(model)}$", card, re.MULTILINE), role
        assert re.search(rf"^model_reasoning_effort: {effort}$", card, re.MULTILINE), role


def test_gpt6_parent_and_review_model_defaults() -> None:
    """Use Sol medium as the normal parent while retaining a Sol review model."""
    config = (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert re.search(r'^model\s*=\s*"gpt-6-sol"$', config, re.MULTILINE)
    assert re.search(r'^review_model\s*=\s*"gpt-6-sol"$', config, re.MULTILINE)
    assert re.search(r'^model_reasoning_effort\s*=\s*"medium"$', config, re.MULTILINE)


def test_active_assignment_record_covers_roles_and_direct_routes() -> None:
    """Keep the active routing record complete without claiming live GPT-6 evidence."""
    evidence = json.loads(
        (
            REPOSITORY_ROOT / "plugins" / "codex-rig" / "runtime" / "calibration" / "accepted-route-evidence.json"
        ).read_text(encoding="utf-8")
    )
    active = evidence["active_assignments"]
    actual = {
        role: (model, effort)
        for model, efforts in active["roles"].items()
        for effort, roles in efforts.items()
        for role in roles
    }
    assert actual == EXPECTED
    assert sum(len(roles) for efforts in active["roles"].values() for roles in efforts.values()) == len(EXPECTED)
    assert active["parent"] == {"model": "gpt-6-sol", "reasoning_effort": "medium"}
    assert active["deep_review"] == {
        "model": "gpt-6-sol",
        "reasoning_effort": "high",
        "activation": "explicit-effort-override",
    }
    assert evidence["active_assignment_basis"]["gpt6_quality_cost_evidence"] == "pending-paired-evaluation"
    assert set(evidence["historical_assignments"]) == {"gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"}
