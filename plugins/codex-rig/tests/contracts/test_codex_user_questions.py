"""Check independently loadable Codex question guidance and its authorization boundaries.

The guidance lives in Markdown (a compact guide, a details file, and skill instructions). Every check reads that text
whitespace-normalized or line by line, so reflowing a paragraph never changes a verdict.
"""

import json
from pathlib import Path

import pytest
from _skill_text import flat, read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
QUESTION_REFERENCE = "shared/codex-user-questions.md"
DETAILS_REFERENCE = "shared/codex-user-questions-details.md"
SKILLS = sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
_REMEDIATE_REFERENCES = ("intake.md", "selection.md", "implementation.md", "completion.md")


def _numbered_item(text: str, number: int) -> str:
    """Return the text of numbered list item *number*, from its marker line to the next item's marker line."""
    marker = f"{number}. "
    collected: list[str] = []
    for line in text.splitlines():
        if collected and line[:1].isdigit() and ". " in line[:4]:
            break
        if collected or line.startswith(marker):
            collected.append(line)
    return flat(" ".join(collected))


def _first_indented_json_example(text: str) -> str:
    """Return the single payload line of the first indented ```json fence, or fail if none has that shape."""
    lines = text.splitlines()
    for index in range(len(lines) - 2):
        indent = lines[index][: len(lines[index]) - len(lines[index].lstrip(" "))]
        fenced = len(indent) >= 2 and lines[index].strip() == "```json"
        if fenced and lines[index + 2] == f"{indent}```" and lines[index + 1].startswith(f"{indent}{{"):
            return lines[index + 1].strip()
    raise AssertionError("no single-line indented json example found")


@pytest.fixture(scope="module")
def compact() -> str:
    """Return the compact question guide text with whitespace normalized."""
    return flat((PLUGIN_ROOT / QUESTION_REFERENCE).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def details() -> str:
    """Return the question details text with whitespace normalized."""
    return flat((PLUGIN_ROOT / DETAILS_REFERENCE).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def remediate() -> str:
    """Return the code-remediate skill text with its trigger-loaded references, whitespace normalized."""
    return flat(read_with_references(PLUGIN_ROOT / "skills/code-remediate/SKILL.md", *_REMEDIATE_REFERENCES))


_OBLIGATIONS = [
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
    "Bind the complete displayed label, including any decision key and suffix, to the unchanged canonical value "
    "before asking; never strip arbitrary text or alter exact-digest confirmation syntax.",
    "Require a visible decision key in the answer when conversation context cannot bind it unambiguously.",
    "Silence, skip, timeout, preselection, an empty result, an example answer, or unrelated text grants no consent.",
    "After supersession, bare `Approve`, indexes, or `all` cannot authorize the replacement; "
    "do not guess from the latest prompt.",
]
_ASYNC_CALIBRATION_CASES = [
    "code-remediate-native-presets-custom-input",
    "code-remediate-native-presets-same-severity",
    "code-remediate-rejected-sync-skips-async",
    "code-remediate-rejected-sync-async-recovery",
]
_SKILL_PARAMS = [pytest.param(skill, id=skill.parent.name) for skill in SKILLS]


class TestQuestionContract:
    """The compact guide and its details file keep the authorization boundaries and delivery rules."""

    @pytest.mark.installed_plugin
    @pytest.mark.parametrize("obligation", _OBLIGATIONS)
    def test_keeps_required_safety_obligations(self, compact: str, details: str, obligation: str) -> None:
        """Protect transport coverage and complete safety clauses, including their negations.

        Each clause is a rule a generated question could silently drop; the check ignores case and line wrapping.
        """
        assert obligation.lower() in f"{compact} {details}".lower()

    @pytest.mark.installed_plugin
    def test_native_discovery_includes_direct_tools_and_one_delivery_owner(self, compact: str, details: str) -> None:
        """Prevent catalog-only discovery and a prose approval followed by a duplicate form.

        Discovery must name directly exposed tools in both documents, and a decision has exactly one delivery owner.
        """
        assert "directly exposed tools" in compact
        assert "directly exposed tools" in details
        assert "one delivery owner" in details
        assert "Do not print the live question or choices before invoking a control" in details
        assert "Absence from `ALL_TOOLS` alone does not establish" in details

    @pytest.mark.installed_plugin
    def test_necessity_precedes_transport_discovery(self, compact: str, details: str) -> None:
        """Keep an authorized internal recovery from opening a blocking consent form.

        Deciding whether to ask comes before choosing or discovering any control, in both documents.
        """
        assert compact.index("Ask only for a genuinely missing decision") < compact.index("discover deferred")
        assert "Routine preparation, internal recovery and completing authorized work need no new consent" in details
        assert details.index("## Decide whether to ask") < details.index("## Choose the control")

    @pytest.mark.installed_plugin
    def test_checkpoint_discovers_deferred_native_tools_before_async(self, compact: str, details: str) -> None:
        """Prevent visible async input from bypassing a deferred native provider in Default mode.

        Item 3 of the compact guide's routing list must discover deferred tools before async, and unknown means
        unsuitable; the details name the discovery mechanics.
        """
        routing = _numbered_item((PLUGIN_ROOT / QUESTION_REFERENCE).read_text(encoding="utf-8"), 3)

        assert "ALL_TOOLS" in routing
        assert "before async" in routing
        assert "unknown means unsuitable" in routing
        assert "functions.exec" in details
        assert "matching metadata, including its complete description" in details
        assert "Do not guess a callable name" in details
        assert "Record the selected tool and the evidence allowing its use before invocation" in details

    @pytest.mark.installed_plugin
    def test_text_agent_message_disqualifies_async_without_resubmitting(self, details: str) -> None:
        """Keep one text-delivered async question pending rather than opening a duplicate control.

        A message carrying both text and structured questions is not two tool calls, so the original stays pending.
        """
        assert "AgentMessage" in details
        assert "both text content and structured `questions`" in details
        assert "does not prove two tool calls" in details
        assert "Keep the original question pending for a typed reply" in details
        assert "Do not submit it again through another control or repeat its menu" in details

    @pytest.mark.installed_plugin
    def test_native_form_is_declared_and_precedes_unverified_async(self, details: str) -> None:
        """Prevent reinstall from retaining the broken async-only Default-mode question route.

        The plugin manifest declares the packaged MCP server, the server file ships, and the details order native
        discovery before async and bind answers to the exact pending decision.
        """
        manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        config = json.loads((PLUGIN_ROOT / ".codex-mcp.json").read_text(encoding="utf-8"))

        assert manifest["mcpServers"] == "./.codex-mcp.json"
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
        assert details.index("2. Otherwise discover") < details.index("3. Use `request_user_input_async` only")
        assert "current host evidence verifies a usable control for the required lifetime" in details
        assert "Consume only an `answered` receipt" in details
        assert "exactly match the pending decision" in details
        assert "A matching digest binds context; it is not user confirmation" in details
        assert "Cached answers do not authorize replay" in details
        assert "omit `options` and include every accepted value and the complete grammar" in details

    def test_async_example_uses_the_exposed_questions_wrapper(self) -> None:
        """Keep the documented async call valid after a rejected top-level title call.

        The first indented json fence in the details file must be a single `questions` wrapper holding one question with
        the Approve and Deny example options.
        """
        example = _first_indented_json_example((PLUGIN_ROOT / DETAILS_REFERENCE).read_text(encoding="utf-8"))

        payload = json.loads(example)

        assert set(payload) == {"questions"}
        assert len(payload["questions"]) == 1
        assert set(payload["questions"][0]) == {"title", "options"}
        assert payload["questions"][0]["options"] == ["example-action: Approve (Recommended)", "example-action: Deny"]

    def test_guide_accepts_explicit_conversational_approval(self, details: str) -> None:
        """Prevent a sole clear merge approval from becoming a repeated syntax question.

        With exactly one pending decision and no competing question, a conversational Approve or Deny is accepted, while
        a superseded scope still needs its key.
        """
        assert "Accept an explicit conversational `Approve` / `Deny`" in details
        assert "exactly one pending decision" in details
        assert "no competing question, superseded scope, or exact-token/digest requirement" in details
        assert "do not ask again solely for a missing decision key" in details
        assert "even when it is the only pending decision" not in details
        assert "Do not append a final or status message after an accepted async question" in details
        assert "This includes an empty final message" in details
        assert "After supersession, bare `Approve`, indexes, or `all` cannot authorize the replacement" in details
        assert "generic Approve never replaces" in details

    def test_remediate_skill_and_compact_guide_accept_conversational_approval(
        self, compact: str, remediate: str
    ) -> None:
        """Keep the remediation skill and compact guide aligned with the details on conversational approval.

        The skill accepts an explicit approval for a sole unchanged merge and drops the old bare-approve refusal.
        """
        assert "Accept an explicit conversational approval for the sole unchanged" in remediate
        assert "unambiguous conversational answer" in compact
        assert "a bare `approve` or `yes` cannot authorize the merge" not in remediate


class TestCalibration:
    """The behavioural calibration fixture keeps async examples honest about native forms."""

    @pytest.mark.parametrize("case_id", _ASYNC_CALIBRATION_CASES)
    def test_async_case_requires_native_discovery_and_verified_host_delivery(self, case_id: str) -> None:
        """Reject positive async examples that silently bypass a suitable native form.

        Each named code-remediate case prompt must mention the packaged native form and verified host rendering.
        """
        fixture = json.loads((PLUGIN_ROOT / "runtime/calibration/behavioral-cases.json").read_text(encoding="utf-8"))
        case = next(case for case in fixture["cases"] if case["id"] == case_id)

        assert "packaged native form" in case["prompt"]
        assert "verified current-host rendering and lifetime" in case["prompt"]


class TestSkillGuidance:
    """Every shipped Codex skill loads the local question guidance and avoids obsolete host assumptions."""

    def test_review_recovery_keeps_fresh_review_choice(self) -> None:
        """Keep missing-evidence recovery choices alongside the interrupted-step exception."""
        skill = (PLUGIN_ROOT / "skills/code-review/SKILL.md").read_text(encoding="utf-8").splitlines()
        recovery = (
            (PLUGIN_ROOT / "skills/code-review/references/report-recovery.md").read_text(encoding="utf-8").splitlines()
        )

        assert any("offer exactly `New review`, `Resume last one`, and `Stop`" in line for line in skill)
        decision_start = recovery.index("## Incomplete review decision")
        assert [line.split("`", 2)[1] for line in recovery[:decision_start] if line.startswith("- `")] == [
            "Continue",
            "Skip",
        ]
        assert [line.split("`", 2)[1] for line in recovery[decision_start:] if line.startswith("- `")] == [
            "New review",
            "Resume last one",
            "Stop",
        ]

    def test_review_preparation_failure_recovers_before_asking(self) -> None:
        """Keep an incorrect helper invocation from becoming an unnecessary async resume question."""
        skill = (PLUGIN_ROOT / "skills/code-review/SKILL.md").read_text(encoding="utf-8").splitlines()
        recovery = (
            (PLUGIN_ROOT / "skills/code-review/references/report-recovery.md").read_text(encoding="utf-8").splitlines()
        )

        assert any("PLUGIN_ROOT/skills/code-review/review_prepare.py" in line for line in skill)
        assert any("continue in the current turn without a resume question" in line for line in skill)
        assert any("recover the invocation before considering a user decision" in line for line in skill)
        assert any(
            "codex-user-questions.md" in line and "discover deferred `ask_user` before considering async" in line
            for line in recovery
        )
        assert any(
            "An accepted async question owns the pending decision: yield without a final or status message" in line
            for line in recovery
        )

    @pytest.mark.installed_plugin
    def test_guide_is_compact_and_ships_its_details(self) -> None:
        """Keep the shipped guide short, rooted in one owner, and paired with its details file.

        The compact guide is loaded by every skill, so its word count is bounded; the skill count pins the roster.
        """
        reference = PLUGIN_ROOT / QUESTION_REFERENCE
        guide = reference.read_text(encoding="utf-8")

        assert len(SKILLS) == 16
        assert len(guide.split()) <= 350
        assert "The root owns user questions" in guide
        assert "codex-user-questions-details.md" in guide
        assert reference.with_name("codex-user-questions-details.md").is_file()

    @pytest.mark.installed_plugin
    @pytest.mark.parametrize("skill", _SKILL_PARAMS)
    def test_loads_local_question_guidance(self, skill: Path) -> None:
        """Prevent a generated question from bypassing the independently shipped policy.

        Each skill links the guide by a relative path that resolves to the shipped file and says to read it before
        asking.
        """
        text = skill.read_text(encoding="utf-8")

        assert f"../../{QUESTION_REFERENCE}" in text
        assert "Before asking" in text
        assert (skill.parent / "../../" / QUESTION_REFERENCE).resolve() == (PLUGIN_ROOT / QUESTION_REFERENCE).resolve()

    @pytest.mark.installed_plugin
    @pytest.mark.parametrize("skill", _SKILL_PARAMS)
    def test_prompts_do_not_force_chat_only_or_yes_no_authorization(self, skill: Path) -> None:
        """Reject obsolete host assumptions without changing factual or exact-token answers.

        Skills must not claim Codex lacks a question tool or ask users for a bare yes/no authorization.
        """
        text = skill.read_text(encoding="utf-8")

        assert "Codex has no `AskUserQuestion`" not in text
        assert "(yes / no)" not in text

    @pytest.mark.installed_plugin
    @pytest.mark.parametrize(
        ("relative", "references"),
        [
            pytest.param(DETAILS_REFERENCE, (), id="details"),
            pytest.param("shared/commit-response-template.md", (), id="commit-response-template"),
            pytest.param("skills/code-remediate/SKILL.md", _REMEDIATE_REFERENCES, id="code-remediate"),
        ],
    )
    def test_consumers_preserve_packaged_native_form_priority(self, relative: str, references: tuple) -> None:
        """Prevent ordinary merge, commit, and oversized menus from bypassing native forms.

        None of the consumers may offer async or plain chat as an equal alternative, and each must route through
        `ask_user`.
        """
        text = flat(read_with_references(PLUGIN_ROOT / relative, *references))

        assert "use async or plain chat" not in text
        assert "use the permitted async control when sync is unavailable" not in text
        assert "using async when sync is unavailable" not in text
        assert "use eligible async" not in text
        assert "ask_user" in text

    @pytest.mark.installed_plugin
    def test_review_recovery_yields_before_blocked_handoff(self) -> None:
        """Keep a pending review answer available after the native question opens.

        In the code-review completion checkpoint an accepted async question yields immediately, before any blocked
        handoff is stated.
        """
        review = flat(read_with_references(PLUGIN_ROOT / "skills/code-review/SKILL.md", "findings-and-completion.md"))
        checkpoint = review.split("Completion checkpoint:", 1)[1].split("### Reviewer validation recovery", 1)[0]

        assert "If a required async question is accepted, yield immediately" in checkpoint
        assert "Do not sleep, poll, or send a final or status handoff" in checkpoint
        assert checkpoint.index("yield immediately") < checkpoint.index("state `Review handoff blocked`")
        assert "Only when an unresolved controlling stop still prevents closure" in checkpoint

    @pytest.mark.installed_plugin
    def test_pr_questions_include_the_bound_clickable_target(self) -> None:
        """Prevent users from having to search prior commentary for a PR decision's target.

        PR-scoped remediation questions carry the PR number and canonical URL, as a Markdown link where supported.
        """
        guide = flat(
            read_with_references(
                PLUGIN_ROOT / "skills/code-remediate/SKILL.md", *_REMEDIATE_REFERENCES, "missing-review-decision.md"
            )
        )

        assert "For PR-scoped decisions, include the PR number and canonical URL inside the question itself" in guide
        assert "[PR #<number>](<canonical PR URL>)" in guide
        assert "plain URL when Markdown links are unsupported" in guide
        assert "frozen target" in guide
        assert "unavailable" in guide

    @pytest.mark.installed_plugin
    def test_scope_question_selects_delivery_before_printing_context(self, remediate: str, details: str) -> None:
        """Prevent a plain scope question in commentary followed by another in the final answer.

        The remediation skill chooses the delivery route before emitting scope context, and the details forbid asking
        the same plain-chat question in commentary and final.
        """
        assert "Choose the delivery route before emitting the scope context" in remediate
        assert "one final response containing the context, report link, and question" in remediate
        assert "template below supplies control content, not a second message" in remediate
        assert "explicit current-host tool contract" in details
        assert "does not establish that the particular call rendered" in details
        assert "Never emit the same plain-chat question in commentary and final" in details


def test_interrupted_review_selects_native_control_before_asking() -> None:
    """Require native Codex question discovery before the Continue/Skip branch or chat fallback."""
    lines = (PLUGIN_ROOT / "skills/code-review/references/report-recovery.md").read_text(encoding="utf-8").splitlines()
    native = next(index for index, line in enumerate(lines) if "For either decision branch, select the control" in line)
    interrupted = next(index for index, line in enumerate(lines) if line == "## Interrupted checks and missed ordering")
    assert native < interrupted
    assert "permitted synchronous `request_user_input` first" in lines[native]
    assert "discover and invoke the native `ask_user` form" in lines[native]
    assert "async or plain chat only after native eligibility is exhausted" in lines[native]
