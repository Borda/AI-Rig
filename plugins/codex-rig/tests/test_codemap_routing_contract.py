"""Pin the documented per-skill Codemap route selection against what the skills actually invoke.

``--query-kind`` is a per-workflow choice rather than a migration every consumer owes, so the contract records each
skill's decision in a table. These checks fail when a skill starts or stops selecting routes without moving its row,
which is the drift that made the adoption gap look like an unfinished rollout instead of a recorded choice.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PLUGIN_ROOT / "shared" / "codemap-contract.md"
SKILLS_ROOT = PLUGIN_ROOT / "skills"
_ADAPTER_INVOCATION = "codemap_adapter.py context"
_ROUTE_ROW = re.compile(
    r"^\|\s*`(?P<skill>[a-z-]+)`\s*\|\s*`(?P<category>[a-z]+)`\s*\|\s*(?P<selection>[^|]+?)\s*\|",
    re.MULTILINE,
)


def _documented_route_selection() -> dict[str, str]:
    """Return `{skill: selection}` parsed from the contract's route-selection table.

    Example:
        >>> _documented_route_selection()["implement"]
        'adaptive — passes `--query-kind`'
    """
    section = CONTRACT_PATH.read_text(encoding="utf-8").split("## Route selection per skill", 1)[1]
    table = section.split("\n\n## ", 1)[0]
    return {match["skill"]: match["selection"] for match in _ROUTE_ROW.finditer(table)}


def _skills_invoking_adapter() -> dict[str, str]:
    """Return `{skill: adapter invocation line}` for every skill that calls the adapter.

    Example:
        >>> "implement" in _skills_invoking_adapter()
        True
    """
    invocations: dict[str, str] = {}
    for skill_file in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
        for line in skill_file.read_text(encoding="utf-8").splitlines():
            if _ADAPTER_INVOCATION in line:
                invocations[skill_file.parent.name] = line
                break
    return invocations


def test_route_selection_table_covers_every_adapter_consuming_skill() -> None:
    """Keep the documented table and the real consumer set identical in both directions."""
    documented = _documented_route_selection()

    assert set(documented) == set(_skills_invoking_adapter())


def test_documented_selection_matches_each_skill_invocation() -> None:
    """Fail when a skill's documented route selection disagrees with its own invocation."""
    documented = _documented_route_selection()
    invocations = _skills_invoking_adapter()

    actual = {skill: "--query-kind" in line for skill, line in invocations.items()}
    expected = {skill: selection.startswith("adaptive") for skill, selection in documented.items()}

    assert actual == expected


def test_standard_batch_skills_are_the_documented_majority_choice() -> None:
    """Pin the recorded split so a silent flip of any row is a test failure, not a doc drift."""
    documented = _documented_route_selection()

    adaptive = sorted(skill for skill, selection in documented.items() if selection.startswith("adaptive"))

    assert adaptive == ["implement", "investigate", "optimize"]


def test_codemap_contract_caps_only_specialist_follow_ups_and_keeps_metadata_limits() -> None:
    """Keep workflow facts uncapped while bounding each specialist's follow-up queries at the adapter's limit.

    The two limits answer different questions: a workflow must finish every structural fact it requires, while one
    specialist re-querying beyond its axis is exactly the per-child fan-out the persisted artifact exists to prevent.
    """
    adapter = _load_adapter()
    contract = CONTRACT_PATH.read_text(encoding="utf-8").lower()

    for phrase in (
        "resolve it once",
        "independent read-only queries may run concurrently",
        "prepared stable index",
        "dependent queries wait",
        "index writes are always serialized",
        "no arbitrary total-call cap for workflow facts",
        "correction retries",
        "same correction failure recurs",
        "sibling queries answering distinct dimensions",
        "metadata-only",
        f"at most {adapter.FOLLOW_UP_QUERY_LIMIT} targeted follow-up queries per workflow run",
        "fourth open structural question becomes recorded coverage gap",
        "--out <run-directory>/codemap-followups/<role>-<nn>.json",
    ):
        assert phrase in contract
    assert "they never re-run adapter" not in contract
    for kind in adapter.FOLLOW_UP_QUERY_KINDS:
        assert f"`{kind}`" in contract


def test_python_scope_probe_is_required_in_change_producing_skills() -> None:
    """Pin the required probe in the three skills it binds and drop every unconditional no-query rule."""
    required = {
        "code-review": "**Structural context (required for Python diffs)**",
        "code-remediate": "**Structural context (required for Python scope)**",
        "implement": "**Structural context (required for Python targets)**",
    }
    texts = {
        skill: (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8") for skill in _skills_invoking_adapter()
    }

    assert {skill: heading in texts[skill] for skill, heading in required.items()} == dict.fromkeys(required, True)
    assert sorted(skill for skill, text in texts.items() if "never fresh" in text) == []
    review_invocations = {skill: _skills_invoking_adapter()[skill] for skill in ("code-review", "code-remediate")}
    assert {skill: "--diff-file" in line and "--root" in line for skill, line in review_invocations.items()} == {
        "code-review": True,
        "code-remediate": True,
    }
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    for code in (
        "codemap-context-missing-for-python-diff",
        "codemap-context-invalid:<field>",
        "codemap-context-skipped-for-python-diff",
    ):
        assert code in contract


def _load_adapter() -> ModuleType:
    """Load the shipped adapter by file path to read its public follow-up limits."""
    spec = importlib.util.spec_from_file_location(
        "codemap_routing_contract_adapter", PLUGIN_ROOT / "shared" / "codemap_adapter.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclass creation resolves string annotations through the defining module's sys.modules entry.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
