"""Pin what the four ``rules/quality-gates.md`` copies say, in each copy.

Four plugins — ``cc_foundry``, ``cc_develop``, ``cc_oss``, ``cc_research`` — each ship a ``rules/quality-gates.md``. All
four are delivered into the single flat ``~/.claude/rules/`` directory and all four load into every session, so each
must state the evidence, routing and proportionality obligations itself. That the copies state the same *set* of named
rules, and that a delta states none its full file lacks, is bookkeeping held by the ``check-policy-parity`` hook
(``scripts/check_policy_parity.py``); what stays here is the content of the instructions.

Each sibling additionally ships a ``rules/quality-gates-delta.md``: the subset ``<plugin>:setup`` delivers in place of
the full file once foundry's copy is already installed, so one shared body loads instead of four. The rule text a plugin
states is therefore spread over two files, and the checks below read them as one text.

Foundry additionally binds its own full-detail Proportional Execution section before planning; only that section is
assembled when its eager mandatory trigger is present. The three satellite policies remain self-contained.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest
from _repo_phrases import missing_phrases

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ("cc_foundry", "cc_develop", "cc_oss", "cc_research")
RULE_NAME = "quality-gates.md"
DELTA_NAME = "quality-gates-delta.md"
# foundry owns the shared body, so it has nothing to defer and ships no delta.
SATELLITES = tuple(plugin for plugin in PLUGINS if plugin != "cc_foundry")
RULE_FILES = {plugin: REPO_ROOT / "plugins" / plugin / "rules" / RULE_NAME for plugin in PLUGINS}
DELTA_FILES = {plugin: REPO_ROOT / "plugins" / plugin / "rules" / DELTA_NAME for plugin in SATELLITES}

#: Sentence in foundry's stub that makes the full Proportional Execution section bind before planning.
_PROPORTIONAL_TRIGGER = (
    "Before planning or implementation, including small parent-owned tasks, resolve + Read "
    "`_full/quality-gates.md` §Proportional Execution; its full policy binds as if inline."
)


def _sections(text: str) -> dict[str, str]:
    """Split a rule file into its ``## `` sections, ignoring headings inside fenced blocks.

    These files quote ``## Confidence`` inside a fenced template. That line is a literal, not a heading, and splitting
    on it would truncate the section that contains it. A heading that recurs has its bodies concatenated rather than
    overwritten: the delta repeats its full file's review-loop heading, and dropping the earlier body would silently
    exempt every rule stated there from the checks.

    Args:
        text: Full file text.

    Returns:
        Section body keyed by heading text, in file order.

    Examples:
        >>> sorted(_sections("## A\\n\\nbody a\\n\\n## B\\n\\nbody b\\n"))
        ['A', 'B']
        >>> _sections("## A\\n\\nfirst\\n\\n## A\\n\\nsecond\\n")["A"]
        'first\\n\\nsecond'
        >>> sorted(_sections("## A\\n```\\n## not a heading\\n```\\n"))
        ['A']
    """
    chunks: list[tuple[str, list[str]]] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            chunks.append((line[3:].strip(), []))
        elif chunks:
            chunks[-1][1].append(line)
    sections: dict[str, str] = {}
    for name, lines in chunks:
        body = "\n".join(lines).strip()
        sections[name] = f"{sections[name]}\n\n{body}" if name in sections else body
    return sections


@cache
def _delivered_text(plugin: str) -> str:
    """Read each plugin's rule, explicitly bound proportional detail, and optional delta.

    Only one of the two is ever delivered, but both are the plugin's own statement of policy, so the rule set it must
    keep in step with its siblings is the union.

    Args:
        plugin: Plugin directory name.

    Returns:
        The rule text, with mandatory bound detail and the plugin's delta appended where applicable.
    """
    text = RULE_FILES[plugin].read_text(encoding="utf-8")
    if plugin == "cc_foundry" and _PROPORTIONAL_TRIGGER in _sections(text)["Proportional Execution"]:
        full = RULE_FILES[plugin].parent / "_full" / RULE_NAME
        body = _sections(full.read_text(encoding="utf-8"))["Proportional Execution"]
        text = f"{text}\n## Proportional Execution\n\n{body}\n"
    delta = DELTA_FILES.get(plugin)
    if delta is not None and delta.is_file():
        text = f"{text}\n{delta.read_text(encoding='utf-8')}"
    return text


class TestPolicyObligations:
    """Each plugin states the evidence, routing and proportionality obligations in its own delivered rule text."""

    @pytest.mark.parametrize(
        "relative",
        [
            *(pytest.param(f"plugins/{plugin}/rules/{RULE_NAME}", id=plugin) for plugin in PLUGINS),
            pytest.param("plugins/codex-rig/shared/native-skill-contract.md", id="codex-rig"),
        ],
    )
    def test_evidence_and_tdd_keep_controlled_first_and_real_acceptance(self, relative: str) -> None:
        """Controlled validation precedes bounded real acceptance in every host policy without weakening coverage.

        Claude plugins carry the policy in ``quality-gates.md`` and Codex Rig in its native-skill contract; a copy that
        drops the ordering would let a real-environment run substitute for the controlled cases.
        """
        policy = (REPO_ROOT / relative).read_text(encoding="utf-8")
        obligations = (
            "Every factual, causal, and completion claim",
            "first run realistic controlled cases",
            "Only after all applicable controlled cases pass",
            "representative real case alongside",
            "An unavailable real environment remains",
            "Never weaken assertions",
            "user-authorized behavior",
            "blocked or unverified",
        )

        assert missing_phrases(policy, obligations) == []
        assert policy.index("first run realistic controlled cases") < policy.index(
            "Only after all applicable controlled cases pass"
        )

    @pytest.mark.parametrize("plugin", PLUGINS)
    def test_implementation_routes_keep_bounded_challenges_and_explicit_loops(self, plugin: str) -> None:
        """Ordered plan acceptance, bounded execution and independent result checks survive in each plugin.

        The small and medium/large implementation routes are exact step chains, so a reordered or dropped step changes
        which challenge a task receives before it ships.
        """
        policy = _delivered_text(plugin)
        small, medium = policy.split("Small implementation:", 1)[1].split("Medium/large:", 1)
        small_steps = (
            "concise plan",
            "brief parent self-challenge (not independent)",
            "accept challenged plan within existing authority",
            "assign parent execution for indivisible work or useful bounded disjoint owners",
            "implement core acceptance",
            "consolidate actual source and handoffs, one bounded independent result challenge, verification and outcome",
        )
        medium_steps = (
            "one scoped plan",
            "independent plan challenge",
            "bounded higher-capability advice only for material unresolved uncertainty",
            "approve challenged plan within existing authority",
            "delegate useful bounded disjoint work or retain indivisible parent execution",
            "implement",
            "consolidate integrated result, independent implementation challenge and verification against every criterion",
        )
        obligations = (
            "Approval never creates a routine user prompt",
            "actual missing authority, protected decisions or an explicit user approval gate",
            "Reuse still-current parent plans and challenges for covered children",
            "Preserve actual independence, permissions, safety, read-only boundaries, explicit user waivers, "
            "single-pass remediation with pre-edit selection, and workflow gates",
            "clear inputs, owner, scope, and acceptance",
            "bounded investigation or decision prerequisites before implementation",
            "a fix plus its regression stays one leaf",
            "detail only the active piece, its dependencies, and interfaces",
            "Work depth-first: complete the active piece's applicable implementation, required independent result "
            "challenge, and acceptance verification before opening the next dependent piece",
            "Delegate useful dependency-ready siblings only with agreed stable interfaces and disjoint ownership",
            "serialize coupled work, parent integration, and shared acceptance",
            "the parent reconciles actual source, handover, and evidence",
            "required source fixes, independent coverage, source-bound or live checks, and final project gates remain "
            "mandatory",
            "An explicitly invoked iterative convergence workflow retains all its existing rules",
        )

        assert " → ".join(small_steps) in small
        assert " → ".join(medium_steps) in medium
        assert missing_phrases(policy, obligations) == []

    def test_foundry_proportional_detail_binds_before_planning_and_parent_execution(self) -> None:
        """Foundry's eager read of the full Proportional Execution section is mandatory before any decision.

        The read must also cover routine parent-owned tasks, so the stub may not call it optional and must state the
        trigger before the first validation step.
        """
        text = RULE_FILES["cc_foundry"].read_text(encoding="utf-8")
        stub = _sections(text)["Proportional Execution"]

        assert _PROPORTIONAL_TRIGGER in stub
        assert stub.index(_PROPORTIONAL_TRIGGER) < stub.index("Validate bounded outcome")
        assert "not needed for routine work" not in text
        assert 'RULE_FULL="$(ls -td ' in text
        assert (
            "Plan → challenge → approve within existing authority → delegate/parent execution → parent consolidate"
            in stub
        )
        assert "Reuse accepted leaf plans; no repeated formal ceremony or new routine approval" in stub


class TestShippedFiles:
    """The parity checks are vacuous if a copy silently disappears, so every expected file must exist."""

    @pytest.mark.parametrize(
        ("name", "files"),
        [pytest.param(RULE_NAME, RULE_FILES, id="rule"), pytest.param(DELTA_NAME, DELTA_FILES, id="delta")],
    )
    def test_every_plugin_ships_the_file(self, name: str, files: dict[str, Path]) -> None:
        """Every plugin that should ship ``name`` still does.

        A satellite losing its delta, or any plugin losing its rule, would make the section and delta checks below
        silently compare fewer copies.
        """
        assert [plugin for plugin, path in files.items() if not path.is_file()] == [], f"missing {name}"


class TestDelta:
    """A satellite's delta only abbreviates what its full file already states."""

    @pytest.mark.parametrize("plugin", SATELLITES)
    def test_output_routing_defers_to_foundry_and_keeps_follow_up_examples(self, plugin: str) -> None:
        """The delta points at foundry's Output Routing instead of repeating it, keeping only plugin additions.

        Foundry's copy is delivered whenever the delta is (``sync_rules`` proves it first), so repeating the routing
        body would re-send it every turn. The follow-up gate examples are the plugin-specific part and must match the
        plugin's own full file.
        """
        full = _sections(RULE_FILES[plugin].read_text(encoding="utf-8"))["Output Routing"]
        delta = _sections(DELTA_FILES[plugin].read_text(encoding="utf-8"))["Output Routing"]
        pointer = f"{plugin}/{DELTA_NAME} §Output Routing must point at foundry's Output Routing"
        examples = [line for line in delta.splitlines() if line.startswith("  - `")]

        assert "foundry §Output Routing" in delta, pointer
        assert "`foundry-quality-gates.md`" in delta, pointer
        assert examples, f"{plugin}/{DELTA_NAME} §Output Routing lost its follow-up gate examples"
        assert [line for line in examples if line not in full] == [], f"{plugin}/{DELTA_NAME} examples differ"
        assert len(delta) < len(full) // 2, f"{plugin}/{DELTA_NAME} §Output Routing repeats the shared body"
