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

from _skill_text import read_with_references

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
        text = (
            read_with_references(skill_file, "source-and-scope.md")
            if skill_file.parent.name == "code-review"
            else read_with_references(skill_file, "selection.md")
            if skill_file.parent.name == "code-remediate"
            else skill_file.read_text(encoding="utf-8")
        )
        for line in text.splitlines():
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

    assert adaptive == ["challenge-resolve", "implement", "investigate", "optimize"]


def test_challenge_resolve_prepares_structural_evidence_before_freezing_review() -> None:
    """Prevent independent challenges from omitting unresolved Python caller and test scope."""
    skill = (SKILLS_ROOT / "challenge-resolve" / "SKILL.md").read_text(encoding="utf-8")
    assert "codemap_adapter.py context --category implementation --query-kind" in skill
    assert "codemap-context.json" in skill
    assert "Reuse still-current caller evidence" in skill
    assert skill.index("codemap_adapter.py context") < skill.index("Before any remediation")


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
        skill: (
            read_with_references(SKILLS_ROOT / skill / "SKILL.md", "source-and-scope.md")
            if skill == "code-review"
            else read_with_references(SKILLS_ROOT / skill / "SKILL.md", "selection.md")
            if skill == "code-remediate"
            else (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
        )
        for skill in _skills_invoking_adapter()
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


def test_review_queries_verified_source_and_keeps_the_collected_diff() -> None:
    """Reject invoking-checkout evidence when a collected review has a different source tree."""
    invocation = _skills_invoking_adapter()["code-review"]
    contract = CONTRACT_PATH.read_text(encoding="utf-8")

    assert "--root <reviewed-source-root> --diff-file <run-directory>/diff.patch" in invocation
    for receipt in ("local-checkout.json.worktree", "review-worktree.json", "review_worktree"):
        assert receipt in invocation
    assert "Never substitute the invoking checkout's index" in contract
    assert "verified reviewed source root" in contract
    assert "never nested review worktree" not in contract
    assert "never the nested review worktree" not in invocation
    assert "never re-runs adapter retroactively" in contract


def test_automatic_provider_preparation_preserves_source_and_serial_ownership() -> None:
    """Permit provider cache bootstrap without turning review into source editing or parallel index writes."""
    contract = CONTRACT_PATH.read_text(encoding="utf-8")

    for phrase in (
        "parent runs the initial query serially",
        "automatically creates or refreshes its own canonical index",
        "working-tree bytes, including unstaged edits and eligible untracked files",
        "Never stage or commit source",
        "never mechanically invoke `scan-codebase`",
        "SCAN_NO_AUTOBUILD=1",
        "index writes are always serialized",
    ):
        assert phrase in contract


def test_older_provider_failure_does_not_switch_review_source_or_retry_incompatibility() -> None:
    """Keep missing-index and wrong-root outcomes honest when the provider lacks automatic preparation."""
    contract = CONTRACT_PATH.read_text(encoding="utf-8")

    assert "older provider" in contract
    assert "missing-index" in contract
    assert "Any residual `root_mismatch` stays `degraded`" in contract
    assert "incompatible provider is never retried within same run" in contract
    assert "Never substitute the invoking checkout's index" in contract


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


def test_provider_degradation_requires_task_specific_coverage_recovery() -> None:
    """Keep static-tool limitations factual without making them permanent review confidence gaps."""
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    recovery = (PLUGIN_ROOT / "shared" / "quality-gates.md").read_text(encoding="utf-8")
    assert "inspect unmapped new files and relevant dynamic references directly" in contract
    assert "Keep the provider artifact degraded" in contract
    assert "a provider limitation alone is not an unresolved review obligation" in recovery
    assert "a required fact remains unproved" in recovery
    assert "Never claim exhaustive dynamic-call coverage" in recovery
