"""Check independently loadable Codex question guidance and its authorization boundaries."""

import json
import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
QUESTION_REFERENCE = "shared/codex-user-questions.md"
SKILLS = sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))


@pytest.mark.installed_plugin
def test_scope_question_selects_delivery_before_printing_context() -> None:
    """Prevent a plain scope question in commentary followed by another in the final answer."""
    skill = (PLUGIN_ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    details = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")
    assert "Choose the delivery route before emitting the scope context" in skill
    assert "one final response containing the context, report link, and question" in skill
    assert "template below supplies control content, not a second message" in skill
    assert "explicit current-host tool contract" in details
    assert "does not establish that the particular call rendered" in details
    assert "Never emit the same plain-chat question in commentary and final" in details


@pytest.mark.installed_plugin
def test_native_question_discovery_includes_direct_tools_and_one_delivery_owner() -> None:
    """Prevent catalog-only discovery and a prose approval followed by a duplicate form."""
    guide = (PLUGIN_ROOT / QUESTION_REFERENCE).read_text(encoding="utf-8")
    details = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")
    assert "directly exposed tools" in guide
    assert "directly exposed tools" in details
    assert "one delivery owner" in details
    assert "Do not print the live question or choices before invoking a control" in details
    assert "Absence from `ALL_TOOLS` alone does not establish" in details


@pytest.mark.parametrize(
    "case_id",
    [
        "code-remediate-native-presets-custom-input",
        "code-remediate-native-presets-same-severity",
        "code-remediate-rejected-sync-skips-async",
        "code-remediate-rejected-sync-async-recovery",
    ],
)
def test_async_calibration_requires_native_discovery_and_verified_host_delivery(case_id: str) -> None:
    """Reject positive async examples that silently bypass a suitable native form."""
    fixture = json.loads((PLUGIN_ROOT / "runtime/calibration/behavioral-cases.json").read_text(encoding="utf-8"))
    case = next(case for case in fixture["cases"] if case["id"] == case_id)
    assert "packaged native form" in case["prompt"]
    assert "verified current-host rendering and lifetime" in case["prompt"]


@pytest.mark.installed_plugin
def test_question_necessity_precedes_transport_discovery() -> None:
    """Keep an authorized internal recovery from opening a blocking consent form."""
    compact = (PLUGIN_ROOT / QUESTION_REFERENCE).read_text(encoding="utf-8")
    details = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")

    assert compact.index("Ask only for a genuinely missing decision") < compact.index("discover deferred")
    assert "Routine preparation, internal recovery and completing authorized work need no new consent" in details
    assert details.index("### Decide whether to ask") < details.index("### Choose the control")


@pytest.mark.installed_plugin
def test_question_checkpoint_discovers_deferred_native_tools_before_async() -> None:
    """Prevent visible async input from bypassing a deferred native provider in Default mode."""
    compact = (PLUGIN_ROOT / QUESTION_REFERENCE).read_text(encoding="utf-8")
    details = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")
    routing = compact.split("3. ", 1)[1].split("\n4. ", 1)[0]

    assert "ALL_TOOLS" in routing
    assert "before async" in routing
    assert "unknown means unsuitable" in routing
    assert "functions.exec" in details
    assert "matching metadata, including its complete description" in details
    assert "Do not guess a callable name" in details
    assert "Record the selected tool and the evidence allowing its use before invocation" in details


@pytest.mark.installed_plugin
def test_text_agent_message_disqualifies_async_without_resubmitting() -> None:
    """Keep one text-delivered async question pending rather than opening a duplicate control."""
    details = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")

    assert "AgentMessage" in details
    assert "both text content and structured `questions`" in details
    assert "does not prove two tool calls" in details
    assert "Keep the original question pending for a typed reply" in details
    assert "Do not submit it again through another control or repeat its menu" in details


@pytest.mark.installed_plugin
def test_native_form_is_declared_and_precedes_unverified_async() -> None:
    """Prevent reinstall from retaining the broken async-only Default-mode question route."""
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["mcpServers"] == "./.codex-mcp.json"
    config = json.loads((PLUGIN_ROOT / ".codex-mcp.json").read_text(encoding="utf-8"))
    assert config == {
        "mcpServers": {
            "codex-rig-input": {
                "command": "python",
                "cwd": ".",
                "args": ["shared/user_questions_mcp.py", "--stdio"],
                "tool_timeout_sec": 3600,
            }
        }
    }
    assert (PLUGIN_ROOT / "shared" / "user_questions_mcp.py").is_file()
    details = (PLUGIN_ROOT / "shared" / "codex-user-questions-details.md").read_text(encoding="utf-8")
    assert details.index("2. Otherwise discover") < details.index("3. Use `request_user_input_async` only")
    assert "current host evidence verifies a usable control for the required lifetime" in details
    assert "Consume only an `answered` receipt" in details
    assert "exactly match the pending decision" in details
    assert "A matching digest binds context; it is not user confirmation" in details
    assert "Cached answers do not authorize replay" in details
    assert "omit `options` and include every accepted value and the complete grammar" in details


@pytest.mark.installed_plugin
def test_every_codex_skill_loads_local_question_guidance() -> None:
    """Prevent a generated question from bypassing the independently shipped policy."""
    assert len(SKILLS) == 15
    reference = PLUGIN_ROOT / QUESTION_REFERENCE.split("#")[0]
    assert reference.is_file()
    guide = reference.read_text(encoding="utf-8")
    assert len(guide.split()) <= 350
    assert "The root owns user questions" in guide
    assert "codex-user-questions-details.md" in guide
    assert reference.with_name("codex-user-questions-details.md").is_file()
    for skill in SKILLS:
        text = skill.read_text(encoding="utf-8")
        assert f"../../{QUESTION_REFERENCE}" in text, skill
        assert "Before asking" in text, skill
        assert (skill.parent / "../../" / QUESTION_REFERENCE.split("#")[0]).resolve() == reference


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    "obligation",
    [
        "request_user_input",
        "request_user_input_async",
        "Tool acceptance does not prove a selectable form was rendered.",
        "A user-reported dismissed question is no longer a usable pending control.",
        "Do not retry the same unsuitable transport for that decision",
        "or later required decisions in the same host until delivery is verified to work again",
        "the final user-facing response of the turn",
        "Synchronous unavailability alone never justifies plain chat.",
        "A failed control call is not a submitted question or an answer.",
        "Do not replay already-delivered report context",
        "A higher-priority host instruction that explicitly mandates plain text remains binding.",
        "Record each control's exposure, permission and input-fit evidence",
        "For each genuinely missing user decision",
        "Use complete actionable values",
        "If no independent work remains, yield",
        "This preference applies to optional and required questions.",
        "permitted for that purpose",
        "all feasible choices",
        "(Recommended)",
        "recommendation is not consent",
        "Approve",
        "Deny",
        "immutable",
        "superseded",
        "canonical",
        "preselection",
        "runtime permission",
        "headless",
        "Do not invent",
        "Completed or superseded keys cannot execute again.",
        (
            "Bind the complete displayed label, including any decision key and suffix, to the unchanged canonical "
            "value before asking; never strip arbitrary text or alter exact-digest confirmation syntax."
        ),
        ("Require a visible decision key in the answer when conversation context cannot bind it unambiguously."),
        (
            "Silence, skip, timeout, preselection, an empty result, an example answer, or unrelated text "
            "grants no consent."
        ),
        (
            "After supersession, bare `Approve`, indexes, or `all` cannot authorize the replacement; "
            "do not guess from the latest prompt."
        ),
    ],
)
def test_question_contract_keeps_required_safety_obligations(obligation: str) -> None:
    """Protect transport coverage and complete safety clauses, including their negations."""
    text = (PLUGIN_ROOT / QUESTION_REFERENCE.split("#")[0]).read_text(encoding="utf-8")
    details = PLUGIN_ROOT / QUESTION_REFERENCE.split("/")[0] / "codex-user-questions-details.md"
    text += "\n" + details.read_text(encoding="utf-8")
    assert obligation.lower() in text.lower()


@pytest.mark.installed_plugin
def test_codex_prompts_do_not_force_chat_only_or_yes_no_authorization() -> None:
    """Reject obsolete host assumptions without changing factual or exact-token answers."""
    for skill in SKILLS:
        text = skill.read_text(encoding="utf-8")
        assert "Codex has no `AskUserQuestion`" not in text, skill
        assert "(yes / no)" not in text, skill


def test_async_question_example_uses_the_exposed_questions_wrapper() -> None:
    """Keep the documented async call valid after a rejected top-level title call."""
    details = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")
    match = re.search(r"(?m)^([ ]{2,})```json\n\1(\{[^\n]+\})\n\1```$", details)
    assert match is not None
    payload = json.loads(match.group(2))
    assert set(payload) == {"questions"}
    assert len(payload["questions"]) == 1
    assert set(payload["questions"][0]) == {"title", "options"}
    assert payload["questions"][0]["options"] == [
        "example-action: Approve (Recommended)",
        "example-action: Deny",
    ]


def test_single_unchanged_merge_accepts_explicit_conversational_approval() -> None:
    """Prevent a sole clear merge approval from becoming a repeated syntax question."""
    guide = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")
    merge = (PLUGIN_ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    compact = (PLUGIN_ROOT / QUESTION_REFERENCE).read_text(encoding="utf-8")
    assert "Accept an explicit conversational `Approve` / `Deny`" in guide
    assert "exactly one pending decision" in guide
    assert "no competing question, superseded scope, or exact-token/digest requirement" in guide
    assert "do not ask again solely for a missing decision key" in guide
    assert "Accept an explicit conversational approval for the sole unchanged" in merge
    assert "unambiguous conversational answer" in compact
    assert "even when it is the only pending decision" not in guide
    assert "Do not append a final or status message after an accepted async question" in guide
    assert "This includes an empty final message" in guide
    assert "a bare `approve` or `yes` cannot authorize the merge" not in merge
    assert "After supersession, bare `Approve`, indexes, or `all` cannot authorize the replacement" in guide
    assert "generic Approve never replaces" in guide


@pytest.mark.installed_plugin
def test_review_recovery_yields_before_blocked_handoff() -> None:
    """Keep a pending review answer available after the native question opens."""
    review = (PLUGIN_ROOT / "skills/code-review/SKILL.md").read_text(encoding="utf-8")
    checkpoint = review.split("Completion checkpoint:", 1)[1].split("### Reviewer validation recovery", 1)[0]

    assert "If a required async question is accepted, yield immediately" in checkpoint
    assert "Do not sleep, poll, or send a final or status handoff" in checkpoint
    assert checkpoint.index("yield immediately") < checkpoint.index("Otherwise state `Review handoff blocked`")


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    "relative",
    ["shared/codex-user-questions-details.md", "shared/commit-response-template.md", "skills/code-remediate/SKILL.md"],
)
def test_question_consumers_preserve_packaged_native_form_priority(relative: str) -> None:
    """Prevent ordinary merge, commit, and oversized menus from bypassing native forms."""
    text = (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
    assert "use async or plain chat" not in text
    assert "use the permitted async control when sync is unavailable" not in text
    assert "using async when sync is unavailable" not in text
    assert "use eligible async" not in text
    assert "ask_user" in text
