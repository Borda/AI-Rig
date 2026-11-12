"""Pin the documented per-skill Codemap route selection against what the skills actually invoke.

``--query-kind`` is a per-workflow choice rather than a migration every consumer owes, so the contract records each
skill's decision in a table. These checks fail when a skill starts or stops selecting routes without moving its row,
which is the drift that made the adoption gap look like an unfinished rollout instead of a recorded choice.

Contract and skill Markdown is read line by line and whitespace-normalized before phrase checks, so reflowing a
paragraph never changes a verdict.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_registered
from _skill_text import flat, read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = PLUGIN_ROOT / "shared" / "codemap-contract.md"
SKILLS_ROOT = PLUGIN_ROOT / "skills"
_ADAPTER_INVOCATION = "codemap_adapter.py context"
_ROUTE_HEADING = "## Route selection per skill"
# Skills whose adapter invocation lives in a trigger-loaded reference rather than SKILL.md.
_INVOCATION_REFERENCE = {"code-review": "source-and-scope.md", "code-remediate": "selection.md"}


def _load_adapter() -> ModuleType:
    """Load the shipped adapter by file path to read its public follow-up limits."""
    return load_module_registered("codemap_routing_contract_adapter", PLUGIN_ROOT / "shared" / "codemap_adapter.py")


ADAPTER = _load_adapter()


def _skill_text(skill: str) -> str:
    """Return a skill's SKILL.md joined with the reference that holds its adapter invocation, if any."""
    skill_file = SKILLS_ROOT / skill / "SKILL.md"
    if skill in _INVOCATION_REFERENCE:
        return read_with_references(skill_file, _INVOCATION_REFERENCE[skill])
    return skill_file.read_text(encoding="utf-8")


def _documented_route_selection() -> dict[str, str]:
    """Return `{skill: selection}` parsed from the contract's route-selection table.

    Example:
        >>> _documented_route_selection()["implement"]
        'adaptive — passes `--query-kind`'
    """
    selection: dict[str, str] = {}
    in_section = False
    for line in CONTRACT_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line.strip() == _ROUTE_HEADING
        elif in_section and line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and cells[0].startswith("`") and cells[0].endswith("`"):
                selection[cells[0].strip("`")] = cells[2]
    return selection


def _skills_invoking_adapter() -> dict[str, str]:
    """Return `{skill: adapter invocation line}` for every skill that calls the adapter.

    Example:
        >>> "implement" in _skills_invoking_adapter()
        True
    """
    invocations: dict[str, str] = {}
    for skill_file in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
        skill = skill_file.parent.name
        line = next((line for line in _skill_text(skill).splitlines() if _ADAPTER_INVOCATION in line), None)
        if line is not None:
            invocations[skill] = line
    return invocations


@pytest.fixture(scope="module")
def contract() -> str:
    """Return the Codemap contract text with whitespace normalized."""
    return flat(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def invocations() -> dict[str, str]:
    """Return the adapter invocation line of every skill that calls the adapter."""
    return _skills_invoking_adapter()


class TestRouteSelectionTable:
    """The contract's per-skill route table keeps its recorded adaptive split."""

    def test_adaptive_skills_are_the_recorded_set(self) -> None:
        """Pin the recorded split so a silent flip of any row is a test failure, not a doc drift.

        Four skills choose routes per decision point; every other consumer keeps the category batch.
        """
        documented = _documented_route_selection()

        adaptive = sorted(skill for skill, selection in documented.items() if selection.startswith("adaptive"))

        assert adaptive == ["challenge-resolve", "implement", "investigate", "optimize"]


_FOLLOW_UP_CAP_PHRASES = [
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
    f"at most {ADAPTER.FOLLOW_UP_QUERY_LIMIT} targeted follow-up queries per workflow run",
    "fourth open structural question becomes recorded coverage gap",
    "--out <run-directory>/codemap-followups/<role>-<nn>.json",
]
_PREPARATION_PHRASES = [
    "parent runs the initial query serially",
    "automatically creates or refreshes its own canonical index",
    "working-tree bytes, including unstaged edits and eligible untracked files",
    "Never stage or commit source",
    "never mechanically invoke `scan-codebase`",
    "SCAN_NO_AUTOBUILD=1",
    "index writes are always serialized",
]
_VERIFIED_SOURCE_PHRASES = [
    "Never substitute the invoking checkout's index",
    "verified reviewed source root",
    "never re-runs adapter retroactively",
]
_OLDER_PROVIDER_PHRASES = [
    "older provider",
    "missing-index",
    "Any residual `root_mismatch` stays `degraded`",
    "incompatible provider is never retried within same run",
    "Never substitute the invoking checkout's index",
]
_PYTHON_DIFF_CODES = [
    "codemap-context-missing-for-python-diff",
    "codemap-context-invalid:<field>",
    "codemap-context-skipped-for-python-diff",
]
_REQUIRED_PROBE_HEADINGS = {
    "code-review": "**Structural context (required for Python diffs)**",
    "code-remediate": "**Structural context (required for Python scope)**",
    "implement": "**Structural context (required for Python targets)**",
}


class TestContractClauses:
    """The Codemap contract keeps the clauses that bound follow-ups, provider preparation, and degradation."""

    @pytest.mark.parametrize(
        "phrase",
        [
            "Map direct, all, or production caller questions to `callers`; "
            "use `blast` only for explicitly transitive caller questions.",
            "`implement`: Use `skip` for exact localized edit with no unresolved structural fact.",
            "`investigate`: Use `skip` when failure is localized and no structural fact is unresolved.",
            "`optimize`: Use `skip` for exact localized optimization with no unresolved structural fact.",
            "matching single route (`central`, `callers`, `blast`, `dependencies`, `test-impact`, or `coupling`) "
            "for one unresolved fact",
            "`standard` for broad or unknown scope",
            "An explicit user or tool request for structural evidence overrides `skip`.",
        ],
    )
    def test_route_section_preserves_adaptive_decisions(self, phrase: str) -> None:
        """Keep caller depth, skill-specific skip conditions, and explicit evidence requests in the bound section."""
        lines = CONTRACT_PATH.read_text(encoding="utf-8").splitlines()
        start = lines.index(_ROUTE_HEADING) + 1
        end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))

        assert phrase in flat("\n".join(lines[start:end]))

    @pytest.mark.parametrize("phrase", _FOLLOW_UP_CAP_PHRASES)
    def test_caps_only_specialist_follow_ups_and_keeps_metadata_limits(self, contract: str, phrase: str) -> None:
        """Keep workflow facts uncapped while bounding each specialist's follow-up queries at the adapter's limit.

        The two limits answer different questions: a workflow must finish every structural fact it requires, while one
        specialist re-querying beyond its axis is exactly the per-child fan-out the persisted artifact exists to
        prevent.
        """
        assert phrase in contract.lower()

    @pytest.mark.parametrize("kind", sorted(ADAPTER.FOLLOW_UP_QUERY_KINDS))
    def test_lists_each_follow_up_query_kind(self, contract: str, kind: str) -> None:
        """Name every follow-up query kind the adapter accepts, so the contract never lags the code.

        The adapter's public kind vocabulary is the source of truth; each kind must appear in backticks.
        """
        assert f"`{kind}`" in contract

    def test_drops_the_obsolete_rerun_claim(self, contract: str) -> None:
        """Keep the retired claim that specialists never re-run the adapter out of the contract.

        Specialists may run bounded follow-ups, so a sentence forbidding any re-run would contradict the cap.
        """
        assert "they never re-run adapter" not in contract.lower()

    @pytest.mark.parametrize("phrase", _PREPARATION_PHRASES)
    def test_provider_preparation_preserves_source_and_serial_ownership(self, contract: str, phrase: str) -> None:
        """Permit provider cache bootstrap without turning review into source editing or parallel index writes.

        The provider may create or refresh its own canonical index, but the parent runs the first query serially and no
        step stages, commits, or scans source on its own.
        """
        assert phrase in contract

    @pytest.mark.parametrize("phrase", _VERIFIED_SOURCE_PHRASES)
    def test_review_queries_the_verified_source_root(self, contract: str, phrase: str) -> None:
        """Require a collected review to query the verified source tree, never the invoking checkout's index.

        A collected review may have a different source tree than the invoking checkout, so the contract must name the
        verified reviewed root and never retroactively re-run the adapter.
        """
        assert phrase in contract

    def test_review_contract_drops_the_nested_worktree_exclusion(self, contract: str) -> None:
        """Keep the retired "never nested review worktree" exclusion out of the contract.

        A nested review worktree is the verified source root, so the old exclusion would send reviews to the wrong
        index.
        """
        assert "never nested review worktree" not in contract

    @pytest.mark.parametrize("phrase", _OLDER_PROVIDER_PHRASES)
    def test_older_provider_failure_does_not_switch_source_or_retry(self, contract: str, phrase: str) -> None:
        """Keep missing-index and wrong-root outcomes honest when the provider lacks automatic preparation.

        An older provider degrades the run with its own detail; it never makes the review switch source trees or retry
        an incompatibility within the same run.
        """
        assert phrase in contract

    @pytest.mark.parametrize("code", _PYTHON_DIFF_CODES)
    def test_names_each_python_diff_failure_code(self, contract: str, code: str) -> None:
        """Name every stop code a Python diff can raise when structural context is missing, invalid, or skipped.

        Review and remediation validators cite these codes, so the contract must define each one.
        """
        assert code in contract

    @pytest.mark.parametrize(
        "phrase",
        [
            "inspect unmapped new files and relevant dynamic references directly",
            "Keep the provider artifact degraded",
        ],
    )
    def test_degradation_requires_task_specific_recovery_in_the_contract(self, contract: str, phrase: str) -> None:
        """Keep static-tool limitations factual and paired with a direct inspection step in the contract.

        A degraded provider artifact stays degraded; the reviewer recovers coverage by reading what the tool missed.
        """
        assert phrase in contract

    @pytest.mark.parametrize(
        "phrase",
        [
            "a provider limitation alone is not an unresolved review obligation",
            "a required fact remains unproved",
            "Never claim exhaustive dynamic-call coverage",
        ],
    )
    def test_degradation_is_not_a_permanent_review_gap(self, phrase: str) -> None:
        """Keep static-tool limits from becoming permanent review confidence gaps in the quality gates.

        Only a required fact that stays unproved is an open obligation, and dynamic-call coverage is never claimed as
        exhaustive.
        """
        recovery = flat((PLUGIN_ROOT / "shared" / "quality-gates.md").read_text(encoding="utf-8"))

        assert phrase in recovery


class TestSkillInvocations:
    """The skills that bind a structural-context probe invoke the adapter the way the contract requires."""

    @pytest.mark.parametrize("skill", ["implement", "investigate", "optimize"])
    def test_adaptive_probe_binds_shared_route_rules(self, invocations: dict[str, str], skill: str) -> None:
        """Require each adaptive probe to load the shared route rules at its existing decision point."""
        line = invocations[skill]
        binding = (
            "Read and apply [Route selection per skill](../../shared/codemap-contract.md#route-selection-per-skill)"
        )

        assert binding in line
        assert (
            line.index(_ADAPTER_INVOCATION) < line.index(binding) < line.index("Per `../../shared/codemap-contract.md`")
        )

    def test_challenge_resolve_prepares_structural_evidence_before_freezing_review(self) -> None:
        """Prevent independent challenges from omitting unresolved Python caller and test scope.

        The skill must run the adaptive adapter query and persist `codemap-context.json` before its remediation gate.
        """
        skill = flat((SKILLS_ROOT / "challenge-resolve" / "SKILL.md").read_text(encoding="utf-8"))

        assert "codemap_adapter.py context --category implementation --query-kind" in skill
        assert "codemap-context.json" in skill
        assert "Reuse still-current caller evidence" in skill
        assert skill.index("codemap_adapter.py context") < skill.index("Before any remediation")

    @pytest.mark.parametrize(
        ("skill", "heading"),
        [pytest.param(skill, heading, id=skill) for skill, heading in _REQUIRED_PROBE_HEADINGS.items()],
    )
    def test_python_scope_probe_is_required_in_change_producing_skills(self, skill: str, heading: str) -> None:
        """Pin the required structural-context heading in the three skills that bind the probe.

        Review, remediation, and implementation each must carry their Python-scope probe requirement verbatim.
        """
        assert heading in flat(_skill_text(skill))

    def test_no_skill_forbids_fresh_queries_unconditionally(self, invocations: dict[str, str]) -> None:
        """Drop every unconditional "never fresh" no-query rule from adapter-consuming skills.

        The probe is required for Python scope, so a blanket ban on fresh queries would contradict it.
        """
        forbidding = [skill for skill in invocations if "never fresh" in flat(_skill_text(skill))]

        assert forbidding == []

    @pytest.mark.parametrize("skill", ["code-review", "code-remediate"])
    def test_review_invocations_pass_root_and_diff_file(self, invocations: dict[str, str], skill: str) -> None:
        """Require review-style invocations to name both the source root and the collected diff file.

        Without `--root` the provider would open the invoking checkout's index; without `--diff-file` it could not map
        the change set.
        """
        line = invocations[skill]

        assert "--diff-file" in line
        assert "--root" in line

    def test_review_invocation_queries_verified_source_and_keeps_the_collected_diff(
        self, invocations: dict[str, str]
    ) -> None:
        """Reject invoking-checkout evidence when a collected review has a different source tree.

        The code-review invocation must point at the reviewed source root with the run's `diff.patch`, cite the worktree
        receipts that verify that root, and never describe the review worktree as excluded.
        """
        invocation = invocations["code-review"]

        assert "--root <reviewed-source-root> --diff-file <run-directory>/diff.patch" in invocation
        assert all(
            receipt in invocation
            for receipt in ("local-checkout.json.worktree", "review-worktree.json", "review_worktree")
        )
        assert "never the nested review worktree" not in invocation
