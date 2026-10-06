"""Hold the four ``rules/quality-gates.md`` copies to the same set of named obligations.

Four plugins — ``cc_foundry``, ``cc_develop``, ``cc_oss``, ``cc_research`` — each ship a ``rules/quality-gates.md``. All
four are delivered into the single flat ``~/.claude/rules/`` directory and all four load into every session, so a rule
written into one copy and not the others governs some agents and not others. That is not a hypothetical: four named
obligations (``Block merge integrity``, ``Deferred work must appear in the delivered artifact``, ``Copy-intent
override``, ``Don't ask what you can't honor``) were each added to foundry in a foundry-scoped commit and never
propagated, so for several releases three quarters of the rule set silently lacked them.

The guard is deliberately *name*-level, not byte-level. The three sibling copies are caveman-compressed rewrites of
foundry's prose and reshape some sections from paragraphs into numbered lists; requiring byte identity would either
forbid that compression or re-expand the siblings and grow every session's context. What must not differ is the *set of
rules*. A new obligation added to one copy alone changes that set and fails here.

Each sibling additionally ships a ``rules/quality-gates-delta.md``: the subset ``<plugin>:setup`` delivers in place of
the full file once foundry's copy is already installed, so one shared body loads instead of four. The rule *set* a
plugin states is therefore spread over two files, and every check below reads them as one text. A delta may only
restate what its own full file already says — it is a delivery optimisation, never a place to put policy, which
:func:`test_a_delta_states_no_rule_its_full_file_lacks` enforces.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ("cc_foundry", "cc_develop", "cc_oss", "cc_research")
RULE_NAME = "quality-gates.md"
DELTA_NAME = "quality-gates-delta.md"
# foundry owns the shared body, so it has nothing to defer and ships no delta.
SATELLITES = tuple(plugin for plugin in PLUGINS if plugin != "cc_foundry")


@pytest.mark.parametrize("plugin", [*PLUGINS, "codex-rig"])
def test_evidence_and_tdd_keep_real_behavior_as_the_oracle(plugin: str) -> None:
    """Keep standalone plugin policy from substituting mocked agreement for real acceptance."""
    policy_path = (
        REPO_ROOT / "plugins" / plugin / "shared/native-skill-contract.md"
        if plugin == "codex-rig"
        else REPO_ROOT / "plugins" / plugin / "rules" / RULE_NAME
    )
    policy = policy_path.read_text(encoding="utf-8")
    for obligation in (
        "Every factual, causal, and completion claim",
        "representative real case alongside the TDD loop",
        "Mocks belong only to subsequent polishing",
        "Never weaken assertions",
        "user-authorized behavior",
        "blocked or unverified",
    ):
        assert obligation in policy, (plugin, obligation)


# ``Output Routing`` states the same obligations in two shapes: foundry inlines the terminal-print
# steps into one prose paragraph, while the siblings break them into nested bullets that carry their
# own bold labels. The names therefore differ without any rule differing. Re-examine this exemption
# if that section is ever restructured to a common shape.
EXEMPT_SECTIONS = frozenset({"output routing"})

_HEADING = re.compile(r"(?m)^## (.+)$")
_NAMED_RULE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\*\*(.+?)\*\*")


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges covered by fenced code blocks.

    These files quote ``## Confidence`` inside a fenced template. That line is a literal, not a
    heading, and splitting on it would truncate the section that contains it.

    Args:
        text: Full file text.

    Returns:
        ``(start, end)`` offsets, one per fenced block, in order.

    Examples:
        >>> _fenced_spans("a\\n```\\nb\\n```\\nc\\n")
        [(2, 12)]
        >>> _fenced_spans("no fence here\\n")
        []
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    opened_at: int | None = None
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            if opened_at is None:
                opened_at = offset
            else:
                spans.append((opened_at, offset + len(line)))
                opened_at = None
        offset += len(line)
    if opened_at is not None:
        spans.append((opened_at, len(text)))
    return spans


def _sections(text: str) -> dict[str, str]:
    """Split a rule file into its ``## `` sections, ignoring headings inside fenced blocks.

    A heading that recurs has its bodies concatenated rather than overwritten: the delta repeats
    its full file's review-loop heading, and dropping the earlier body would silently exempt
    every rule stated there from the parity guard.

    Args:
        text: Full file text.

    Returns:
        Section body keyed by heading text, in file order.

    Examples:
        >>> sorted(_sections("## A\\n\\nbody a\\n\\n## B\\n\\nbody b\\n"))
        ['A', 'B']
        >>> _sections("## A\\n\\nfirst\\n\\n## A\\n\\nsecond\\n")["A"]
        'first\\n\\nsecond'
    """
    spans = _fenced_spans(text)
    headings = [m for m in _HEADING.finditer(text) if not any(a <= m.start() < b for a, b in spans)]
    out: dict[str, str] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        name = heading.group(1).strip()
        body = text[heading.end() : end].strip()
        out[name] = f"{out[name]}\n\n{body}" if name in out else body
    return out


def _section_key(heading: str) -> str:
    """Normalise a heading so the same section matches across copies.

    Headings carry per-copy trigger parentheticals, e.g. ``Pre-Handover Check (trigger: ...)``.
    Only the part before the first parenthesis identifies the section.

    Args:
        heading: Raw heading text.

    Returns:
        Lowercased identifying prefix.

    Examples:
        >>> _section_key("Pre-Handover Check (trigger: a named gap)")
        'pre-handover check'
        >>> _section_key("Link Verification")
        'link verification'
    """
    return heading.split("(")[0].strip().lower()


def _named_rules(body: str) -> set[str]:
    """Bold labels of the obligations a section states, one per list item.

    Args:
        body: Section body.

    Returns:
        Lowercased labels with any trailing colon removed.

    Examples:
        >>> sorted(_named_rules("- **Report before fixing**: state it\\n- plain bullet\\n"))
        ['report before fixing']
    """
    found = set()
    for line in body.splitlines():
        match = _NAMED_RULE.match(line)
        if match:
            found.add(match.group(1).strip().rstrip(":").lower())
    return found


def _rule_files() -> dict[str, Path]:
    """Locate every shipped ``rules/quality-gates.md``.

    Returns:
        Path keyed by plugin directory name.
    """
    return {plugin: REPO_ROOT / "plugins" / plugin / "rules" / RULE_NAME for plugin in PLUGINS}


def _delta_files() -> dict[str, Path]:
    """Locate every satellite's ``rules/quality-gates-delta.md``.

    Returns:
        Path keyed by plugin directory name.
    """
    return {plugin: REPO_ROOT / "plugins" / plugin / "rules" / DELTA_NAME for plugin in SATELLITES}


def _delivered_text(plugin: str) -> str:
    """Everything one plugin states about quality gates: its full rule plus its delta.

    Only one of the two is ever delivered, but both are the plugin's own statement of policy, so
    the rule set it must keep in step with its siblings is the union.

    Args:
        plugin: Plugin directory name.

    Returns:
        The full rule's text, with the delta's appended when the plugin ships one.
    """
    text = _rule_files()[plugin].read_text(encoding="utf-8")
    delta = _delta_files().get(plugin)
    if delta is not None and delta.is_file():
        text = f"{text}\n{delta.read_text(encoding='utf-8')}"
    return text


def test_every_plugin_ships_the_shared_rule_file() -> None:
    """The parity check below is vacuous if a copy silently disappears."""
    missing = [plugin for plugin, path in _rule_files().items() if not path.is_file()]
    assert not missing, f"missing {RULE_NAME}: {missing}"


def test_every_satellite_ships_a_delta() -> None:
    """A satellite losing its delta would make the delta checks below silently vacuous."""
    missing = [plugin for plugin, path in _delta_files().items() if not path.is_file()]
    assert not missing, f"missing {DELTA_NAME}: {missing}"


def _shared_sections() -> dict[str, dict[str, str]]:
    """Section bodies keyed by normalised section name, then by plugin, for sections in 2+ copies.

    Returns:
        Mapping of section key to ``{plugin: body}``.
    """
    per_plugin = {plugin: _sections(_delivered_text(plugin)) for plugin in _rule_files()}
    shared: dict[str, dict[str, str]] = {}
    for plugin, sections in per_plugin.items():
        for heading, body in sections.items():
            shared.setdefault(_section_key(heading), {})[plugin] = body
    return {key: copies for key, copies in shared.items() if len(copies) > 1}


@pytest.mark.parametrize("section", sorted(set(_shared_sections()) - EXEMPT_SECTIONS))
def test_a_shared_section_states_the_same_named_rules_in_every_copy(section: str) -> None:
    """A named obligation added to one copy alone is a policy split, not a local edit.

    Args:
        section: Normalised section name present in more than one plugin's rule file.
    """
    copies = _shared_sections()[section]
    by_plugin = {plugin: _named_rules(body) for plugin, body in copies.items()}
    everywhere = set.intersection(*by_plugin.values())
    anywhere = set.union(*by_plugin.values())
    split = {
        rule: sorted(plugin for plugin, rules in by_plugin.items() if rule in rules)
        for rule in sorted(anywhere - everywhere)
    }
    assert not split, (
        f"§{section} states rules in some copies and not others: {split}. "
        f"Propagate the rule to every plugin in {sorted(copies)}, or, if the difference is "
        f"deliberate, add the section to EXEMPT_SECTIONS with the reason."
    )


def test_the_exemption_list_names_only_sections_that_exist() -> None:
    """A stale exemption would silently disable the guard for a section that was since renamed."""
    unknown = EXEMPT_SECTIONS - set(_shared_sections())
    assert not unknown, f"EXEMPT_SECTIONS names sections no copy ships: {sorted(unknown)}"


def test_the_guard_covers_more_than_one_section() -> None:
    """Guards against a parsing regression that would silently reduce coverage to nothing."""
    covered = set(_shared_sections()) - EXEMPT_SECTIONS
    assert len(covered) >= 5, f"only {len(covered)} shared sections parsed: {sorted(covered)}"


@pytest.mark.parametrize("plugin", SATELLITES)
def test_a_delta_states_no_rule_its_full_file_lacks(plugin: str) -> None:
    """A delta abbreviates delivery, so policy written only there would reach nobody on a standalone install.

    Args:
        plugin: Satellite plugin directory name.
    """
    full = _named_rules(_rule_files()[plugin].read_text(encoding="utf-8"))
    delta = _named_rules(_delta_files()[plugin].read_text(encoding="utf-8"))
    assert not delta - full, (
        f"{plugin}/{DELTA_NAME} states rules absent from {RULE_NAME}: {sorted(delta - full)}. "
        f"Add them to the full file first — the delta is only delivered when foundry's copy is installed."
    )


@pytest.mark.parametrize("plugin", SATELLITES)
def test_a_delta_carries_its_output_routing_section_verbatim(plugin: str) -> None:
    """Output Routing is the one section the delta must supply, so it may not drift from the full file.

    Args:
        plugin: Satellite plugin directory name.
    """
    full = _sections(_rule_files()[plugin].read_text(encoding="utf-8"))["Output Routing"]
    delta = _sections(_delta_files()[plugin].read_text(encoding="utf-8"))["Output Routing"]
    assert delta == full, f"{plugin}/{DELTA_NAME} §Output Routing differs from {RULE_NAME}; copy it across verbatim"
