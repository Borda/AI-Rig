"""Pin the documented per-skill Codemap route selection against what the skills actually invoke.

``--query-kind`` is a per-workflow choice rather than a migration every consumer owes, so the contract records each
skill's decision in a table. These checks fail when a skill starts or stops selecting routes without moving its row,
which is the drift that made the adoption gap look like an unfinished rollout instead of a recorded choice.
"""

from __future__ import annotations

import re
from pathlib import Path


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


def test_codemap_contract_bounds_query_execution_without_cap_or_metadata_shortcut() -> None:
    """Keep the consumer contract explicit about stable reads, reusable evidence, and metadata limits."""
    contract = CONTRACT_PATH.read_text(encoding="utf-8").lower()

    for phrase in (
        "resolve it once",
        "independent read-only queries may run concurrently",
        "prepared stable index",
        "dependent queries wait",
        "index writes are always serialized",
        "no arbitrary total-call cap",
        "correction retries",
        "same correction failure recurs",
        "sibling queries answering distinct dimensions",
        "metadata-only",
    ):
        assert phrase in contract
    assert "maximum three" not in contract
