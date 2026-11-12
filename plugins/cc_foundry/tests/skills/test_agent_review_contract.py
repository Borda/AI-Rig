"""Protect content-review dispatch, evidence collection, and config-review handoffs."""

from pathlib import Path

import pytest

_AGENTS = Path(__file__).resolve().parents[2] / "agents"


def _section(agent: str, tag: str) -> list[str]:
    """Read an agent section without relying on prose wrapping."""
    lines = (_AGENTS / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(f"<{tag}>")
    end = lines.index(f"</{tag}>", start)
    return lines[start + 1 : end]


def test_creator_review_dispatch_precedes_generation_guards() -> None:
    """A draft review must bypass generation's outline-path and output-writing guards."""
    lines = _section("creator", "workflow")
    dispatch = next(index for index, line in enumerate(lines) if "**Mode dispatch**" in line)
    generation = lines.index("## Generation branch")
    review = lines.index("## Review branch")
    assert dispatch < generation < review
    assert any("inline" in line and "outline" in line for line in lines[review:])
    assert any("Do not generate" in line and "artifact" in line for line in lines[review:])
    assert any("location" in line and "severity" in line and "fix" in line for line in lines[review:])


def test_creator_all_stop_paths_preserve_blocked_reason_in_envelope() -> None:
    """Missing outlines, scaffolds, or an already-authored video cannot masquerade as timeouts."""
    lines = _section("creator", "workflow")
    delivery = next(line for line in lines if "**Stop delivery**" in line)
    assert all(marker in delivery for marker in ("BREAKING", "MISSING", "BLOCKED"))
    assert '"status": "blocked"' in delivery
    assert '"reason"' in delivery
    assert "no artifact" in delivery


def test_creator_constraints_gate_can_return_decision_without_question_tool() -> None:
    """A CFP subagent must return its question and choices when user interaction is unavailable."""
    line = next(line for line in _section("creator", "outline-contract") if "Constraints section empty" in line)
    assert "Subagent context" in line
    assert '"status": "needs_decision"' in line
    assert "needs_decision: true" in line
    assert "question" in line
    assert "options" in line
    assert "no artifact" in line


def test_creator_format_gate_uses_same_decision_status() -> None:
    """Both creator decision gates must expose a machine-readable status to their caller."""
    line = next(line for line in _section("creator", "workflow") if "Format [missing/unrecognized" in line)
    assert '"status": "needs_decision"' in line
    assert "question" in line
    assert "options" in line


def test_creator_envelope_keeps_confidence_and_divergence_out_of_artifact() -> None:
    """Envelope-only delivery retains quality evidence without polluting the requested content."""
    lines = _section("creator", "workflow")
    delivery = next(line for line in lines if "**Quality delivery**" in line)
    assert all(field in delivery for field in ("confidence", "notes", "Gaps", "divergence"))
    assert "Never append" in delivery
    assert "artifact" in delivery
    assert any("notes" in line for line in _section("creator", "creative-posture"))


@pytest.mark.parametrize("agent", ["creator", "curator"])
def test_quality_gate_reference_resolves_in_user_install(agent: str) -> None:
    """Quality instructions must locate the global rule installed by foundry setup."""
    lines = _section(agent, "workflow")
    assert any("~/.claude/rules/foundry-quality-gates.md" in line for line in lines)
    assert not any("`.claude/rules/foundry-quality-gates.md`" in line for line in lines)


def test_challenger_can_collect_bounded_evidence_without_caller_command_list() -> None:
    """Evidence grounding may run safe throwaway probes while project mutations remain forbidden."""
    line = next(line for line in _section("challenger", "role") if "Bash restricted" in line)
    assert "read-only verification probes on throwaway inputs" in line
    assert "Never runs a candidate command in a real shell" in line
    assert "foreground" in line
    assert "timeout" in line


def test_challenger_targeted_codemap_values_are_bound_in_same_call() -> None:
    """Targeted codemap queries cannot depend on a previous Bash call's vanished variables."""
    lines = _section("challenger", "codemap-context")
    binding = next(line for line in lines if "Before running the block" in line)
    assert "TARGET_MODULE" in binding
    assert "TARGET_FN" in binding
    assert "same Bash call" in binding
    assert "empty strings" in binding
    assert "index" in binding


def test_curator_resolves_public_plugin_name_to_prefixed_source_directory() -> None:
    """A public plugin name must reach cc-prefixed source directories as well as exact names."""
    line = next(line for line in _section("curator", "workflow") if "**Scope resolution**" in line)
    assert "plugins/<token>/" in line
    assert "plugins/cc_<token>/" in line
    assert "ambiguous" in line
    assert "resolved directory" in line


def test_curator_url_cache_hashes_the_complete_url() -> None:
    """URLs sharing a long prefix must never reuse one another's fetched page."""
    line = next(line for line in _section("curator", "workflow") if "Per-URL cache" in line)
    assert "SHA-256" in line
    assert "complete URL" in line
    assert "64" in line
    assert "UTF-8" in line
    assert "first 32" not in line


def test_curator_cache_is_optional_in_strict_read_only_audits() -> None:
    """Caching may not violate a caller's no-write boundary or overwrite unread evidence."""
    lines = _section("curator", "workflow")
    default = next(line for line in lines if line.startswith("Default:"))
    assert ".cache/curator/" in default
    assert "stricter" in default
    assert "in-memory" in default
    cache = next(line for line in lines if "Per-URL cache" in line)
    assert "Read any existing" in cache
    assert "before overwriting" in cache
    assert not any(".cache/gh/" in line for line in lines)


def test_curator_schema_check_distinguishes_ignored_plugin_fields() -> None:
    """Recognized user-agent fields cannot be assumed effective for plugin agents."""
    lines = _section("curator", "evaluation-criteria")
    ignored = next(line for line in lines if "ignored in plugin agents" in line)
    assert all(field in ignored for field in ("permissionMode", "hooks", "mcpServers"))
    freshness = next(line for line in _section("curator", "workflow") if "WebFetch current" in line)
    assert "https://code.claude.com/docs/en/sub-agents" in freshness
    assert "https://code.claude.com/docs/en/skills" in freshness


def test_curator_validates_agent_and_skill_references_with_optional_bridge() -> None:
    """Skill dispatches and optional bridge references must receive the same validation as agents."""
    line = next(line for line in _section("curator", "workflow") if "For cross-refs:" in line)
    assert "bridge:" in line
    assert "<agents-dir>" in line
    assert "<skills-dir>" in line
    assert "separately" in line
    assert "<dir>)" in line
    assert "bridge" in line
    assert "available" in line
    assert "skills/" in line


def test_curator_confidence_uses_evidence_without_minimum_scores() -> None:
    """Inline-only or envelope-only evidence must not force an unsupported confidence score."""
    lines = _section("curator", "output-format")
    assert not any("floor 0.90" in line or "floor 0.92" in line for line in lines)
    assert any("lowest sub-finding confidence" in line and "ceiling" in line for line in lines)
    assert any("report real score" in line for line in lines)


def test_curator_accepts_caller_directed_judgment_checks() -> None:
    """Audit and adversarial handoffs must not be declined as structural-review-only work."""
    line = next(line for line in _section("curator", "routing-boundaries") if "adversarial challenge" in line)
    assert "caller-directed" in line
    assert "handoff" in line
    assert "judgment checks" in line


def test_curator_direct_invocation_allows_single_plugin_scope() -> None:
    """The direct-invocation note must admit the plugin scope supported by Step 1."""
    line = next(line for line in _section("curator", "notes") if "**System-wide sweep**" in line)
    assert "targeted file or single-plugin checks" in line


def test_curator_maps_priorities_to_requested_severity_envelopes() -> None:
    """Audit consumers must receive severity labels instead of unrecognized P-number keys."""
    lines = _section("curator", "output-format")
    mapping = next(line for line in lines if "**Severity mapping**" in line)
    assert all(tier in mapping for tier in ("security", "critical", "high", "medium", "low"))
    assert all(priority in mapping for priority in ("P1", "P2", "P3", "P4", "P5"))
    assert "caller" in mapping
    assert "counts" in mapping


def test_creator_file_only_guards_do_not_decline_inline_reviews() -> None:
    """Inline draft reviews must not be vetoed by generation-only routing and source restrictions."""
    routing = _section("creator", "routing-boundaries")
    skip = next(line for line in routing if line.startswith("- SKIP also"))
    source = next(line for line in _section("creator", "notes") if "**Input source**" in line)
    assert "generation only" in skip
    assert "reviews" in source
    assert "inline" in source


def test_curator_skip_guard_preserves_caller_directed_judgment_scope() -> None:
    """The behavior-review skip clause cannot override an explicit workflow judgment handoff."""
    skip = next(line for line in _section("curator", "routing-boundaries") if line.startswith("- SKIP:"))
    assert "except caller-directed handoff and judgment checks" in skip


def test_challenger_target_binding_keeps_missing_index_fallback_reachable() -> None:
    """Unavailable codemap or an unindexed target must not prevent the guarded preflight from running."""
    binding = next(line for line in _section("challenger", "codemap-context") if "Before running the block" in line)
    assert "unavailable" in binding
    assert "unindexed" in binding
    assert "empty strings" in binding
