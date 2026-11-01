"""Contract tests for code-review routing, specialist-manifest, and verified PR-source artifacts.

Covers the deterministic ``review_routing.py`` producer, the standalone ``validate_artifacts.py`` validator, and the
Markdown instructions (SKILL.md, references, shared contracts) that tell a reviewer how to drive them. Markdown is read
as whitespace-normalised prose so reflowing a paragraph cannot break an assertion that names a phrase.
"""

from __future__ import annotations

import functools
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple
from unittest import mock

import pytest
from _artifact_files import read_json
from _modules import load_module
from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CODE_REVIEW_SKILL = PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md"
CODE_REVIEW_REFERENCES = CODE_REVIEW_SKILL.parent / "references"
CODE_REVIEW_RESULT_TEMPLATE = PLUGIN_ROOT / "skills" / "code-review" / "result-template.json"
HELPER_CLI_CONTRACT = PLUGIN_ROOT / "shared" / "helper-cli-contract.md"
REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
ROUTING_HELPER = PLUGIN_ROOT / "skills" / "code-review" / "review_routing.py"
PARALLEL_EXECUTION_TESTS = PLUGIN_ROOT / "tests" / "loop" / "test_parallel_execution.py"
COMPLETION_GATE_TESTS = Path(__file__).with_name("test_review_completion_gate.py")

BASE_OID = "a" * 40
HEAD_OID = "b" * 40
WORKTREE_MISMATCH = r"pr-source-(review-worktree-invalid|review-gates-worktree-mismatch)"


# Loaded once: the shipped validator is a standalone script whose module state tests never mutate, and monkeypatched
# attributes are restored per test.
_VALIDATOR = load_module("code_review_routing_validator", REVIEW_VALIDATOR)


@functools.cache
def _parallel_execution_tests() -> ModuleType:
    """Load the shared rollout fixtures without making production code depend on tests."""
    return load_module("codex_rig_parallel_execution_tests", PARALLEL_EXECUTION_TESTS)


@functools.cache
def _completion_gate_tests() -> ModuleType:
    """Load the assessed-PR fixture builder owned by the completion-gate tests."""
    return load_module("code_review_completion_fixture", COMPLETION_GATE_TESTS)


def _flatten(text: str) -> str:
    """Collapse all whitespace runs so a phrase matches wherever the source paragraph wraps."""
    return " ".join(text.split())


@functools.cache
def _skill_text(*references: str) -> str:
    """Return SKILL.md plus the named reference files as whitespace-normalised prose."""
    return _flatten(read_with_references(CODE_REVIEW_SKILL, *references))


@functools.cache
def _document_text(path: Path) -> str:
    """Return one Markdown document as whitespace-normalised prose."""
    return _flatten(path.read_text(encoding="utf-8"))


def _assert_states(text: str, *phrases: str) -> None:
    """Assert every phrase occurs in ``text``, naming all missing phrases at once instead of dumping the document."""
    missing = [phrase for phrase in phrases if phrase not in text]
    assert not missing, f"missing phrases: {missing}"


def _write_json(path: Path, payload: object) -> Path:
    """Write one JSON artifact and return its path."""
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _signals(*enabled: str) -> dict[str, bool]:
    """Return every routing signal mapped to whether it is among the ``enabled`` names."""
    return {name: name in enabled for name in _VALIDATOR.ROUTING_SIGNALS}


def _size_evidence(files: int, changed_lines: int, *extra: str) -> list[str]:
    """Return the mechanical size evidence the routing record must carry, plus any extra entries."""
    return [f"files={files}", f"changed_lines={changed_lines}", "unknown_size_rows=0", *extra]


def _routing_record(
    signals: dict[str, bool],
    *,
    evidence: list[str],
    roles: list[str] | tuple[str, ...] = (),
    risk_tier: str = "LOCAL",
    mechanical_tier: str = "TRIVIAL",
    reasons: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Build a schema-one routing record whose per-signal and per-role evidence are valid unless overridden."""
    return {
        "schema_version": 1,
        "risk_tier": risk_tier,
        "mechanical_risk_tier": mechanical_tier,
        "mechanical_risk_evidence": evidence,
        "signals": signals,
        "signal_evidence": {name: ["Observed routing evidence."] for name in signals},
        "triggered_roles": list(roles),
        "trigger_reasons": {role: ["Observed routing evidence."] for role in roles} if reasons is None else reasons,
    }


def _write_inventory(out: Path, *, files: bytes = b"", untracked: bytes = b"", numstat: bytes = b"") -> None:
    """Write the three collected path inventories routing validation derives mechanical evidence from."""
    (out / "files.txt").write_bytes(files)
    (out / "untracked.txt").write_bytes(untracked)
    (out / "numstat.txt").write_bytes(numstat)


def _write_routing(out: Path, record: dict[str, Any]) -> Path:
    """Write ``review-routing.json`` and return its path."""
    return _write_json(out / "review-routing.json", record)


def _run_routing_helper(out: Path) -> subprocess.CompletedProcess[str]:
    """Run the shipped routing producer as a real child process against ``out``."""
    return subprocess.run(
        [sys.executable, str(ROUTING_HELPER), "--out", str(out)], capture_output=True, text=True, check=False
    )


class TestSkillEntry:
    """SKILL.md keeps the entry-level review contract that every later gate depends on."""

    @pytest.mark.parametrize(
        "phrase",
        [
            pytest.param("shared [Proportional Execution]", id="proportional-execution-link"),
            pytest.param("source, coverage, artifact, and completion gates still govern", id="gates-still-govern"),
            pytest.param('"done_when": "blocking issues are identified with gate decision"', id="completion-condition"),
            pytest.param("references/findings-and-completion.md#quality-gates", id="quality-gates-anchor"),
        ],
    )
    def test_entry_states_contract_phrase(self, phrase: str) -> None:
        """SKILL.md states each entry-contract phrase: proportional execution, completion condition, quality gates.

        Lightweight analysis must stay available without weakening the detailed gates, the caller's completion condition
        must survive in the review input, and native-configuration reviews must reach their calibration.
        """
        assert phrase in _skill_text()

    def test_scope_reference_binds_direct_commit_reviews_to_revision_comparison(self) -> None:
        """Direct commit reviews freeze the same Git comparison that was requested."""
        scope = _document_text(CODE_REVIEW_REFERENCES / "source-and-scope.md")

        _assert_states(
            scope,
            "For a three-dot range, freeze its merge base",
            "for a single commit, freeze its parent (or empty-tree object for the initial commit)",
        )

    def test_orchestration_distinguishes_direct_and_paged_reviews(self) -> None:
        """Shared orchestration names the direct schema separately from the page-only review protocol.

        The retired wording "new schema-eight native route" must not return and blur the two routes.
        """
        orchestration = _document_text(PLUGIN_ROOT / "shared" / "specialist-orchestration.md")

        _assert_states(orchestration, "schema-ten direct", "schema-eight paged")
        assert "Code Review's new schema-eight native route" not in orchestration

    def test_completion_reference_offers_copyable_recovery_requests(self) -> None:
        """A blocked handoff includes copyable recovery requests when no further decision is needed."""
        completion = _document_text(CODE_REVIEW_REFERENCES / "findings-and-completion.md")

        _assert_states(
            completion,
            "Resume the code review of <target> from <exact run path> and finish the missing checks.",
            "Run a fresh code review of <target>.",
        )


class TestSkillRoutingText:
    """The producer-side routing instructions stay aligned with what the validator enforces."""

    def test_routing_synchronization_precedes_specialist_manifest(self) -> None:
        """The workflow runs the deterministic routing helper before it writes the specialist manifest.

        The producer must derive mechanical evidence with the same helper the validator later checks against.
        """
        skill = _skill_text("routing.md", "historical-routes.md")
        invocation = "PLUGIN_ROOT/bin/python PLUGIN_ROOT/skills/code-review/review_routing.py --out <run-directory>"

        assert invocation in skill
        assert skill.index(invocation) < skill.index("`<run-directory>/specialist-manifest.json`")

    def test_requires_list_valued_routing_evidence_and_reasons(self) -> None:
        """The skill requires list-valued evidence and reasons so a bare string cannot strand a review.

        A valid-looking string value would otherwise leave the run as an unpromoted candidate.
        """
        skill = _skill_text("routing.md")

        _assert_states(
            skill,
            "non-empty JSON `list[str]` value for each true/false decision",
            "non-empty JSON `list[str]` value",
            "Bare strings are invalid.",
        )

    def test_schema_three_role_and_sol_provenance_is_documented(self) -> None:
        """The skill documents schema-three role-card and explicit Sol-selection provenance fields."""
        skill = _skill_text("routing.md", "historical-routes.md", "batched-review.md")

        _assert_states(
            skill,
            "`specialist-manifest.json` uses schema version 3",
            "`role_card_sha256`",
            "`sol_selection`",
            "`source=explicit-user-selection`",
            "`parent_event_id`",
            "`selection_sha256`",
        )

    def test_result_template_records_role_card_digest(self) -> None:
        """The result template carries the role-card digest placeholder for each specialist pass."""
        template = read_json(CODE_REVIEW_RESULT_TEMPLATE)

        assert template["metadata"]["specialist_passes"][0]["role_card_sha256"] == "sha256 of installed ROLE.md"

    def test_runtime_gate_and_execution_labels_are_truthful(self) -> None:
        """The skill binds execution to the shared runtime gate and keeps planned fan-out from being called parallel."""
        skill = _skill_text("historical-routes.md", "batched-review.md")

        _assert_states(
            skill,
            "`<run-directory>/execution-plan.json`",
            "`<run-directory>/execution-manifest.json`",
            "`consumer_id=code-review`",
            "`parallel` only when",
            "`independent-spawned`",
            "`serial-fallback`",
        )

    def test_pr_snapshot_is_rebuilt_before_findings(self) -> None:
        """Assessed PR handoffs rebuild a compact snapshot from current artifacts, not from stale chat context."""
        skill = _skill_text("findings-and-completion.md")

        _assert_states(
            skill,
            "`PR Snapshot` for every assessed `scope=pr` review",
            "`pr.json`, `pr-routing.json`, and `gates.json`",
            "`pr.json.statusCheckRollup`",
            "An absent or empty rollup is `unavailable`, never `passing`",
            "`fix`, `feat`, `refactor`, `perf`, `docs`, `ci`, `chore`, `test`, or `mixed`",
            "`approve`, `minor changes`, `major revision`, `insufficient, but a good initiative`, `block`, "
            "or `no source judgment`",
            "before any findings",
        )


class TestPrArtifactPathContract:
    """PR identity promotion is ordered, safe, and topology-neutral downstream."""

    def test_promotes_after_authoritative_collection(self) -> None:
        """Path promotion happens only after current-branch PR identity is collected.

        A run promoted before collection could be filed under an identity that was never confirmed.
        """
        skill = _skill_text("source-and-scope.md")
        collection = "For PR scope, inspect `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/collect_pr.py --help`"
        promotion = "create_run.py --skill code-review --promote-pr-run <run-directory>"

        assert skill.index(collection) < skill.index(promotion)
        _assert_states(
            skill,
            ".reports/codex/code-review/pr-<number>/run-<NNN>/",
            "Use the printed promoted path literally for every later helper",
            "It is not an assessed PR review and must not be promoted.",
        )

    def test_rejects_raw_argument_directory_names(self) -> None:
        """Prompts, paths, URLs, and credentials never leak into artifact directory names."""
        contract = _document_text(HELPER_CLI_CONTRACT)

        _assert_states(
            contract,
            ".reports/codex/<skill>/<canonical-safe-identity>/run-<NNN>/",
            "Never serialize raw prompts, paths, URLs, refs, credentials, or arbitrary arguments",
            "otherwise they use the generated timestamp",
        )

    def test_result_template_uses_selected_run_directory(self) -> None:
        """One result template serves both local and promoted PR run directories."""
        template = read_json(CODE_REVIEW_RESULT_TEMPLATE)

        assert template["artifact_path"] == "<run-directory>/result.json"
        assert template["metadata"]["final_handoff"]["handoff_path"] == "<run-directory>/final-handoff.json"
        assert template["metadata"]["specialist_manifest"] == "<run-directory>/specialist-manifest.json"


class TestValidatorParsers:
    """Small parsers and registries inside the standalone validator."""

    def test_action_table_rows_stop_at_next_heading(self) -> None:
        """The remediation-table parser returns the canonical section's rows and stops at the next heading.

        A regex over-escape once rejected a valid table; the following section's table must stay excluded.
        """
        notes = (
            "# Review\n\n## Review Findings and Merge Blocks\n\n"
            "| Finding / area | Required change | Evidence | Status |\n"
            "| --- | --- | --- | --- |\n"
            "| Parser | Accept the canonical table. | `review-notes.md` | Required |\n\n"
            "## Confidence Calibration\n\n| This | is | not | a finding |\n"
        )

        rows = _VALIDATOR._action_table_rows(notes)

        assert rows == [
            ["Finding / area", "Required change", "Evidence", "Status"],
            ["---", "---", "---", "---"],
            ["Parser", "Accept the canonical table.", "`review-notes.md`", "Required"],
        ]

    def test_event_payloads_normalize_item_completed_activity(self) -> None:
        """Sub-agent activity in the current ``item_completed`` rollout shape becomes a flat activity record.

        Review provenance reads the parent rollout; the flat record must carry identity, kind, path, and timestamps.
        """
        item = {
            "type": "SubAgentActivity",
            "id": "activity-1",
            "kind": "started",
            "agent_path": "/root/review_qa",
            "agent_thread_id": "child-1",
        }
        payload = {"type": "item_completed", "started_at_ms": 1234, "completed_at_ms": 1250, "item": item}

        activities = _VALIDATOR._event_payloads([{"type": "event_msg", "payload": payload}], "sub_agent_activity")

        assert activities == [
            {
                "event_id": "activity-1",
                "kind": "started",
                "agent_path": "/root/review_qa",
                "agent_thread_id": "child-1",
                "started_at_ms": 1234,
                "completed_at_ms": 1250,
            }
        ]

    def test_role_roster_covers_every_packaged_specialist(self) -> None:
        """The executable review roster matches the packaged role cards.

        Only the curator and delegation lead are outside review; the software engineer, QA specialist, and challenger
        are always eligible, and every other role needs a conditional signal.
        """
        packaged_roles = {path.parent.name for path in (PLUGIN_ROOT / "roles").glob("*/ROLE.md")}

        assert _VALIDATOR.ALL_MANIFEST_ROLES == packaged_roles - {"curator", "delegation-lead"}
        assert set(_VALIDATOR.CONDITIONAL_SIGNALS) == _VALIDATOR.ALL_MANIFEST_ROLES - {
            "sw-engineer",
            "qa-specialist",
            "challenger",
        }

    def test_packaged_role_card_supplies_runtime_contract(self) -> None:
        """The shipped role card alone supplies a role's runtime contract; no source agent config is consulted.

        Plugin-only installs have no project ``.codex/agents`` directory, so provenance must be grounded in ROLE.md.
        """
        role_card = PLUGIN_ROOT / "roles" / "qa-specialist" / "ROLE.md"

        contract = _VALIDATOR._load_role_card(PLUGIN_ROOT / "roles", "qa-specialist")

        assert contract == {
            "approval_policy": "on-request",
            "model_reasoning_effort": "medium",
            "role_card_sha256": hashlib.sha256(role_card.read_bytes()).hexdigest(),
            "role_id": "qa-specialist",
            "sandbox_mode": "workspace-write",
        }
        assert 'project_root / ".codex" / "agents"' not in REVIEW_VALIDATOR.read_text(encoding="utf-8")


class TestRoutingHelper:
    """The ``review_routing.py`` producer, run as the real child process the workflow invokes."""

    @pytest.mark.parametrize("source_path", ["src/widget.py", "src/widget.pyi"])
    def test_replaces_manual_mechanical_evidence_idempotently(self, tmp_path: Path, source_path: str) -> None:
        """File and line arithmetic is derived from collected evidence, and a second run changes nothing.

        The record starts with model-authored, wrong mechanical evidence (329 lines, TRIVIAL); the helper must replace
        it from the collected numstat (343 lines, LOCAL) and leave later runs byte-identical.
        """
        signals = _signals("bug_fix", "test_or_error_path")
        _write_inventory(
            tmp_path,
            files=f"{source_path}\ntests/test_widget.py\n".encode(),
            numstat=f"51\t14\t{source_path}\n278\t0\ttests/test_widget.py\n".encode(),
        )
        routing_path = _write_routing(
            tmp_path, _routing_record(signals, evidence=_size_evidence(2, 329), roles=["qa-specialist", "sw-engineer"])
        )

        first = _run_routing_helper(tmp_path)
        first_bytes = routing_path.read_bytes()
        second = _run_routing_helper(tmp_path)

        routing = json.loads(first_bytes)
        assert (first.returncode, second.returncode) == (0, 0), first.stderr + second.stderr
        assert routing_path.read_bytes() == first_bytes
        assert routing["mechanical_risk_tier"] == "LOCAL"
        assert routing["mechanical_risk_evidence"] == _size_evidence(2, 343)
        assert routing["signals"] == signals
        assert _VALIDATOR._validate_routing(tmp_path, "LOCAL") == {"qa-specialist", "sw-engineer"}

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            pytest.param(
                lambda routing: routing.pop("signals"), "review-routing-signal-set-mismatch", id="omitted-container"
            ),
            pytest.param(
                lambda routing: routing["signals"].pop("bug_fix"),
                "review-routing-signal-set-mismatch",
                id="missing-boolean",
            ),
            pytest.param(
                lambda routing: routing["signals"].update(unknown_signal=False),
                "review-routing-signal-set-mismatch",
                id="unknown-boolean",
            ),
            pytest.param(
                lambda routing: routing["signals"].update(bug_fix=1),
                "review-routing-signals-not-boolean",
                id="integer-is-not-boolean",
            ),
        ],
    )
    def test_rejects_invalid_signals_without_writing(
        self, tmp_path: Path, mutate: Callable[[dict[str, Any]], object], expected: str
    ) -> None:
        """Malformed semantic signal decisions fail the producer and leave the record untouched.

        An omitted, partial, extended, or non-Boolean signal set must not pass the mechanical producer unchanged.
        """
        routing = {"schema_version": 1, "risk_tier": "LOCAL", "signals": _signals()}
        mutate(routing)
        routing_path = tmp_path / "review-routing.json"
        original = json.dumps(routing).encode("utf-8")
        routing_path.write_bytes(original)

        result = _run_routing_helper(tmp_path)

        assert result.returncode != 0
        assert expected in result.stderr
        assert routing_path.read_bytes() == original

    @pytest.mark.parametrize(
        "routing",
        [
            pytest.param({"declared_risk_tier": "LOCAL"}, id="legacy-alias-without-risk-tier"),
            pytest.param({"risk_tier": "UNKNOWN"}, id="invalid-risk-tier"),
        ],
    )
    def test_rejects_missing_or_invalid_risk_tier_without_writing(
        self, tmp_path: Path, routing: dict[str, str]
    ) -> None:
        """A missing or unknown reviewer risk tier fails before specialist work and leaves the evidence unchanged."""
        routing_path = _write_json(tmp_path / "review-routing.json", routing)
        original = routing_path.read_bytes()

        completed = _run_routing_helper(tmp_path)

        assert completed.returncode != 0
        assert "invalid-risk-tier" in completed.stderr
        assert routing_path.read_bytes() == original

    def test_skill_example_passes_installed_helper_and_validator(self, tmp_path: Path) -> None:
        """The nested routing example printed in the skill passes the installed helper and ordinary validation.

        The documented producer schema must not drift from what validation accepts.
        """
        raw_skill = read_with_references(CODE_REVIEW_SKILL, "routing.md")
        section = raw_skill.split("The following neutral example shows the complete nested shape", 1)[1]
        example = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
        (tmp_path / "review-routing.json").write_text(json.dumps(example), encoding="utf-8", newline="\n")

        result = _run_routing_helper(tmp_path)

        assert result.returncode == 0, result.stderr
        assert _VALIDATOR._validate_routing(tmp_path, "TRIVIAL") == set()
        assert "Never infer omitted signals as `false` or move them to top level." in _flatten(raw_skill)
        assert "needs no new human routing choice" in _flatten(section)

    def test_validator_cli_exposes_manifest_only_preflight(self) -> None:
        """The validator's command line offers ``--manifest-only`` so the workflow can run the preflight alone."""
        completed = subprocess.run(
            [sys.executable, str(REVIEW_VALIDATOR), "--help"], capture_output=True, text=True, check=False
        )

        assert completed.returncode == 0
        assert "--manifest-only" in completed.stdout


def _sol_selection_fields(role: str) -> dict[str, Any]:
    """Return the explicit-selection evidence a Sol-pinned role needs, or no extra fields for any other role."""
    evidence = {"source": "explicit-user-selection", "parent_event_id": "event-1", "selection_sha256": "a" * 64}
    return {"sol_selection": {role: evidence}} if role in _VALIDATOR.SOL_ROLES else {}


_AXIS_ROUTES = sorted({**_VALIDATOR.CONDITIONAL_SIGNALS, "challenger": "explicit_adversarial"}.items())


class TestRoutingValidation:
    """``_validate_routing`` derives the required reviewer roster from collected path evidence."""

    def test_test_only_python_review_does_not_require_software_engineer(self, tmp_path: Path) -> None:
        """A review of only a changed test file stays with QA instead of claiming a production source pass."""
        _write_inventory(tmp_path, files=b"tests/test_widget.py\n", numstat=b"1\t1\ttests/test_widget.py\n")
        record = _routing_record(_signals("test_or_error_path"), evidence=_size_evidence(1, 2), roles=["qa-specialist"])
        _write_routing(tmp_path, record)

        assert _VALIDATOR._validate_routing(tmp_path, "LOCAL") == {"qa-specialist"}

    def test_git_quoted_untracked_python_requires_software_engineer(self, tmp_path: Path) -> None:
        """A non-ASCII untracked production filename cannot bypass the required source reviewer.

        Git C-quotes such names (``"src/calf\\303\\251.py"``); the quoted form must still be recognised as Python.
        """
        _write_inventory(tmp_path, untracked=b'"src/calf\\303\\251.py"\n')
        _write_routing(tmp_path, _routing_record(_signals(), evidence=_size_evidence(1, 0)))

        with pytest.raises(SystemExit, match="sw-engineer"):
            _VALIDATOR._validate_routing(tmp_path, "LOCAL")

    @pytest.mark.parametrize(
        ("risk_tier", "extra_signals", "extra_roles", "diagnostic"),
        [
            pytest.param("BROAD", (), (), "review-routing-tier-underclassified:HIGH_RISK:BROAD", id="dependent-errors"),
            pytest.param(
                "HIGH_RISK",
                (),
                ("challenger",),
                "review-routing-mechanical-signals-false:axis_cicd_steward",
                id="false-ci-signal",
            ),
            pytest.param(
                "HIGH_RISK",
                ("axis_cicd_steward",),
                ("cicd-steward",),
                "review-routing-triggered-role-mismatch",
                id="missing-challenger",
            ),
        ],
    )
    def test_first_rejection_reports_every_requirement_without_rewriting(
        self,
        tmp_path: Path,
        risk_tier: str,
        extra_signals: tuple[str, ...],
        extra_roles: tuple[str, ...],
        diagnostic: str,
    ) -> None:
        """The first rejection lists the complete effective tier, signals, and roles and leaves the record alone.

        A CI file plus a documentation migration is high-risk; reporting only the first defect would spend the single
        bounded setup repair on the tier alone while dependent signal and role errors remained.
        """
        _write_inventory(
            tmp_path,
            files=b".github/check.py\ndocs/migration.md\n",
            numstat=b"1\t1\t.github/check.py\n1\t1\tdocs/migration.md\n",
        )
        roles = sorted({"doc-scribe", "qa-specialist", "sw-engineer", "web-explorer", *extra_roles})
        evidence = _size_evidence(2, 4, "high_risk_paths=docs/migration.md")
        record = _routing_record(
            _signals("axis_doc_scribe", "axis_web_explorer", *extra_signals),
            evidence=evidence,
            roles=roles,
            risk_tier=risk_tier,
            mechanical_tier="HIGH_RISK",
        )
        original = _write_routing(tmp_path, record).read_bytes()

        with pytest.raises(SystemExit) as rejected:
            _VALIDATOR._validate_routing(tmp_path, risk_tier)

        message = str(rejected.value)
        assert message.startswith(diagnostic)
        assert "minimum risk_tier=HIGH_RISK" in message
        assert "mandatory signals=axis_cicd_steward,axis_doc_scribe" in message
        assert "required roles=challenger,cicd-steward,doc-scribe,qa-specialist,sw-engineer,web-explorer." in message
        assert (tmp_path / "review-routing.json").read_bytes() == original

    def test_retains_stronger_authored_risk_and_semantic_axis(self, tmp_path: Path) -> None:
        """An authored HIGH_RISK tier and web-explorer axis stand even when the paths need only a trivial tier.

        Nonmechanical review requirements must not be downgraded just because the collected path is ``notes.txt``.
        """
        _write_inventory(tmp_path, files=b"notes.txt\n", numstat=b"1\t1\tnotes.txt\n")
        roles = ["challenger", "qa-specialist", "web-explorer"]
        record = _routing_record(
            _signals("axis_web_explorer"), evidence=_size_evidence(1, 2), roles=roles, risk_tier="HIGH_RISK"
        )
        original = _write_routing(tmp_path, record).read_bytes()

        accepted = _VALIDATOR._validate_routing(tmp_path, "HIGH_RISK")

        assert accepted == set(roles)
        assert (tmp_path / "review-routing.json").read_bytes() == original

    @pytest.mark.parametrize(
        ("role", "signal", "selection"),
        [pytest.param(role, signal, _sol_selection_fields(role), id=role) for role, signal in _AXIS_ROUTES],
    )
    def test_each_axis_routes_only_its_own_specialist(
        self, tmp_path: Path, role: str, signal: str, selection: dict[str, Any]
    ) -> None:
        """Each non-Python specialist is selected by its own axis signal without pulling in unrelated roles.

        Sol-pinned roles additionally carry explicit-selection evidence, built per role at collection time.
        """
        _write_inventory(tmp_path, files=b"notes.txt\n", numstat=b"1\t1\tnotes.txt\n")
        record = _routing_record(
            _signals(signal), evidence=_size_evidence(1, 2), roles=[role], reasons={role: [f"{signal} applies."]}
        )
        _write_routing(tmp_path, record | selection)

        assert _VALIDATOR._validate_routing(tmp_path, "LOCAL") == {role}

    @pytest.mark.parametrize(
        "reasons",
        [
            pytest.param("one bare reason", id="bare-string"),
            pytest.param(["valid reason", 3], id="non-string-member"),
        ],
    )
    def test_rejects_trigger_reasons_that_are_not_nonempty_string_lists(self, tmp_path: Path, reasons: object) -> None:
        """A bare string or a list with a non-string member fails the routing preflight."""
        _write_inventory(tmp_path, files=b"src/widget.txt\n", numstat=b"1\t1\tsrc/widget.txt\n")
        record = _routing_record(
            _signals("behavior_change"),
            evidence=_size_evidence(1, 2),
            roles=["qa-specialist"],
            reasons={"qa-specialist": reasons},
        )
        _write_routing(tmp_path, record)

        with pytest.raises(SystemExit, match="review-routing-trigger-reason-values-invalid"):
            _VALIDATOR._validate_routing(tmp_path, "LOCAL")

    @pytest.mark.parametrize(
        ("role", "signal"),
        [
            pytest.param("solution-architect", "axis_solution_architect", id="architecture-needs-selection"),
            pytest.param("security-auditor", "axis_security_auditor", id="security-needs-selection"),
        ],
    )
    def test_sol_axis_requires_explicit_selection_evidence(self, tmp_path: Path, role: str, signal: str) -> None:
        """An architecture or security label cannot automatically select a Sol-pinned role.

        Without explicit-selection evidence the validator must reject the route and leave the record untouched.
        """
        _write_inventory(tmp_path, files=b"src/widget.py\n", numstat=b"1\t1\tsrc/widget.py\n")
        record = _routing_record(
            _signals(signal),
            evidence=_size_evidence(1, 2),
            roles=[role],
            reasons={role: ["Explicitly restricted review axis."]},
        )
        original = _write_routing(tmp_path, record).read_bytes()

        with pytest.raises(SystemExit, match=f"review-routing-sol-selection-missing:{role}"):
            _VALIDATOR._validate_routing(tmp_path, "LOCAL")

        assert (tmp_path / "review-routing.json").read_bytes() == original


class TestPathInventoryRouting:
    """Quoted, literal, and newline-bearing paths resolve to one consistent source inventory."""

    def test_pr_literal_quoted_names_are_not_git_unquoted(self, tmp_path: Path) -> None:
        """PR metadata writes names that merely look C-quoted (``"auth"``) as literal paths.

        Both names are literal text files, so risk stays TRIVIAL and no Python source review is demanded.
        """
        from collect_pr import _review_artifacts

        paths = ['"auth"', '"bad\\x.py"']
        payload = {"state": "OPEN", "body": "review", "files": [{"path": path} for path in paths]}
        _write_json(tmp_path / "pr.json", payload)
        _review_artifacts(
            payload,
            [],
            tmp_path,
            base_repo="owner/repo",
            base_host="github.com",
            thread_error=None,
            pr_metadata_transport="gh",
        )
        patch = "".join(
            f"diff --git {json.dumps('a/' + path)} {json.dumps('b/' + path)}\n"
            f"--- /dev/null\n+++ {json.dumps('b/' + path)}\n"
            for path in paths
        )
        (tmp_path / "diff.patch").write_text(patch, encoding="utf-8", newline="\n")
        # No files.txt: the PR file inventory is derived from pr.json and the patch, not from a local listing.
        (tmp_path / "untracked.txt").write_bytes(b"")
        (tmp_path / "numstat.txt").write_bytes(b"")
        _write_routing(tmp_path, _routing_record(_signals(), evidence=_size_evidence(2, 0)))

        tier, evidence, required_signals = _VALIDATOR.derive_mechanical_risk(tmp_path)

        assert tier == "TRIVIAL"
        assert evidence == _size_evidence(2, 0)
        assert not required_signals
        assert _VALIDATOR._validate_routing(tmp_path, "LOCAL") == set()
        assert _VALIDATOR._python_diff_paths(tmp_path) == []

    def test_local_git_quoted_names_decode_once(self, tmp_path: Path) -> None:
        """Git's escaped quotes and backslashes in a local listing decode to the same literal paths as PR metadata."""
        from review_routing import read_review_paths

        (tmp_path / "files.txt").write_bytes(b'"\\"auth\\""\n"\\"bad\\\\x.py\\""\n')

        paths = read_review_paths(tmp_path, "files.txt")

        assert paths == {'"auth"', '"bad\\x.py"'}
        assert _VALIDATOR.derive_mechanical_risk(tmp_path)[0] == "TRIVIAL"

    def test_stray_pr_json_cannot_hide_git_quoted_python(self, tmp_path: Path) -> None:
        """An unrelated PR artifact cannot suppress the required local source reviewer or Codemap.

        ``pr.json`` is empty, yet the local listing names a C-quoted Python file, which still needs sw-engineer.
        """
        _write_json(tmp_path / "pr.json", {})
        _write_inventory(tmp_path, files=b'"module_\\303\\251.py"\n')
        _write_routing(tmp_path, _routing_record(_signals(), evidence=_size_evidence(1, 0)))

        with pytest.raises(SystemExit, match="sw-engineer"):
            _VALIDATOR._validate_routing(tmp_path, "LOCAL")
        assert _VALIDATOR._python_diff_paths(tmp_path) == ["module_é.py"]

    @pytest.mark.parametrize("newline", [pytest.param("\n", id="lf"), pytest.param("\r\n", id="crlf")])
    def test_pr_newline_path_uses_patch_inventory(self, tmp_path: Path, newline: str) -> None:
        """A PR filename containing a newline remains one Python source path and still requires sw-engineer.

        The line-oriented listing would split the name, so the patch inventory is the authority, whatever the listing's
        own line endings are.
        """
        path = "app\ntest_unit.py"
        _write_json(tmp_path / "pr.json", {})
        (tmp_path / "files.txt").write_text(path + "\n", encoding="utf-8", newline=newline)
        (tmp_path / "diff.patch").write_text(
            f"diff --git {json.dumps('a/' + path)} {json.dumps('b/' + path)}\n"
            f"--- /dev/null\n+++ {json.dumps('b/' + path)}\n",
            encoding="utf-8",
            newline="\n",
        )
        _write_routing(tmp_path, _routing_record(_signals(), evidence=_size_evidence(1, 0)))

        with pytest.raises(SystemExit, match="sw-engineer"):
            _VALIDATOR._validate_routing(tmp_path, "LOCAL")
        assert _VALIDATOR._python_diff_paths(tmp_path) == [path]
        assert _VALIDATOR.derive_mechanical_risk(tmp_path)[1][0] == "files=1"

    @pytest.mark.parametrize(
        "stage_listing",
        [
            pytest.param(lambda out: None, id="missing-listing"),
            pytest.param(lambda out: (out / "files.txt").write_text("notes.txt\n", encoding="utf-8"), id="mismatch"),
        ],
    )
    def test_final_admission_rejects_missing_or_mismatched_patch_inventory(
        self, tmp_path: Path, stage_listing: Callable[[Path], object]
    ) -> None:
        """Retained paged evidence cannot hide a patch path when the final reviewer roster is checked.

        The patch names ``module.py``; with no listing, or one naming only ``notes.txt``, the inventory is invalid.
        """
        (tmp_path / "diff.patch").write_text(
            "diff --git a/module.py b/module.py\n--- a/module.py\n+++ b/module.py\n", encoding="utf-8"
        )
        stage_listing(tmp_path)
        environment = _VALIDATOR._ReviewEnvironment(tmp_path, "parent", tmp_path)

        with pytest.raises(SystemExit, match="review-source-file-inventory-invalid"):
            _VALIDATOR._validate_specialist_manifest(tmp_path, {}, tmp_path / "result.json", {}, "LOCAL", environment)


def _substituted_manifest(
    tmp_path: Path, *roles: str, schema_version: int = 2
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Create a minimal substitute manifest; schema three and later also bind each pass to its packaged role card."""
    review_input = b"diff --git a/widget.py b/widget.py\n"
    (tmp_path / "diff.patch").write_bytes(review_input)
    passes: list[dict[str, object]] = []
    for role in roles:
        output = tmp_path / "specialists" / f"{role}.md"
        output.parent.mkdir(exist_ok=True)
        output.write_text(f"role_id: {role}\n\nBounded {role} evidence.\n", encoding="utf-8")
        item: dict[str, object] = {
            "role": role,
            "axis": "tests",
            "mode": "substituted",
            "trigger": "fixture trigger",
            "confidence": 0.9,
            "blocking_findings": 0,
            "output_path": str(output),
        }
        if schema_version >= 3:
            item["role_card_sha256"] = hashlib.sha256(
                (PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes()
            ).hexdigest()
        passes.append(item)
    manifest: dict[str, object] = {
        "schema_version": schema_version,
        "review_run_id": "review-run",
        "parent_thread_id": "parent-thread",
        "review_input_sha256": hashlib.sha256(review_input).hexdigest(),
        "passes": passes,
    }
    return manifest, passes


def _admit_manifest(out: Path, manifest: dict[str, object], passes: list[dict[str, object]]) -> dict[str, Any]:
    """Run manifest-entry admission for exactly the roles named by ``passes`` under the fixed parent thread."""
    roles = {str(item["role"]) for item in passes}
    return _VALIDATOR._validate_manifest_entries(out, manifest, passes, roles, out, "parent-thread", out)


class TestSpecialistManifest:
    """Manifest entries bind each specialist pass to its role, evidence file, and packaged role card."""

    def test_rejects_reused_specialist_output_paths(self, tmp_path: Path) -> None:
        """Two roles cannot claim the same evidence file."""
        manifest, passes = _substituted_manifest(tmp_path, "qa-specialist", "challenger")
        passes[1]["output_path"] = passes[0]["output_path"]

        with pytest.raises(SystemExit, match="manifest-reused-output-path"):
            _admit_manifest(tmp_path, manifest, passes)

    @pytest.mark.parametrize(
        ("schema_version", "output"),
        [
            pytest.param(2, "generic note\n", id="weak-generic-note"),
            pytest.param(2, "# QA Specialist\n\nBounded review.\n", id="display-name-alias"),
            pytest.param(2, "role_id: challenger\n\nBounded review for qa-specialist.\n", id="wrong-role-id"),
            pytest.param(2, "Generic note about qa-specialist, not a role binding.\n", id="incidental-mention"),
            pytest.param(5, "# qa-specialist\n\nBounded review evidence.\n", id="schema-five-legacy-heading"),
            pytest.param(5, "qa-specialist:\n\nBounded review evidence.\n", id="schema-five-legacy-label"),
        ],
    )
    def test_rejects_substitute_without_exact_role_id(self, tmp_path: Path, schema_version: int, output: str) -> None:
        """Parent-only evidence must identify its manifest role and carry substantive content.

        A display-name alias, another role's id, an incidental mention, or a weak generic note is not a role binding;
        schema five additionally rejects the historical heading and label forms in favour of a ``role_id`` field.
        """
        manifest, passes = _substituted_manifest(tmp_path, "qa-specialist", schema_version=schema_version)
        Path(str(passes[0]["output_path"])).write_text(output, encoding="utf-8")

        with pytest.raises(SystemExit, match="manifest-substitute-output-not-role-bound:qa-specialist"):
            _admit_manifest(tmp_path, manifest, passes)

    @pytest.mark.parametrize(
        ("schema_version", "first_line"),
        [
            pytest.param(2, "role_id: qa-specialist", id="explicit-field"),
            pytest.param(2, "# qa-specialist", id="historical-heading"),
            pytest.param(2, "qa-specialist:", id="historical-label"),
            pytest.param(3, "role_id: qa-specialist", id="schema-three-packaged-card"),
            pytest.param(5, "role_id: qa-specialist", id="schema-five-explicit-field"),
        ],
    )
    def test_accepts_substitute_with_exact_role_id(self, tmp_path: Path, schema_version: int, first_line: str) -> None:
        """A substitute whose first line names its exact role is admitted for the schemas that allow that form.

        Schema two still reads the historical heading and label; schemas three and five bind the packaged role card and
        need only the explicit ``role_id`` field.
        """
        manifest, passes = _substituted_manifest(tmp_path, "qa-specialist", schema_version=schema_version)
        Path(str(passes[0]["output_path"])).write_text(f"{first_line}\n\nBounded review evidence.\n", encoding="utf-8")

        admitted = _admit_manifest(tmp_path, manifest, passes)

        assert admitted == {"qa-specialist": passes[0]}

    @pytest.mark.parametrize("role", ["solution-architect", "security-auditor"])
    def test_requires_explicit_selection_evidence_for_sol_roles(self, tmp_path: Path, role: str) -> None:
        """A Sol-pinned pass without immutable explicit-selection evidence is blocked."""
        manifest, passes = _substituted_manifest(tmp_path, role)

        with pytest.raises(SystemExit, match=f"manifest-sol-selection-missing:{role}"):
            _admit_manifest(tmp_path, manifest, passes)

    def test_rejects_unstructured_sol_selection_evidence(self, tmp_path: Path) -> None:
        """Sol selection needs a source, event identity, and digest instead of a self-authored label like ``"yes"``."""
        manifest, passes = _substituted_manifest(tmp_path, "solution-architect")
        manifest["sol_selection"] = {"solution-architect": "yes"}

        with pytest.raises(SystemExit, match="manifest-sol-selection-invalid:solution-architect"):
            _admit_manifest(tmp_path, manifest, passes)

    def test_schema_three_rejects_substituted_role_card(self, tmp_path: Path) -> None:
        """Schema three binds each pass to the exact packaged role card, so a wrong digest is rejected."""
        manifest, passes = _substituted_manifest(tmp_path, "qa-specialist", schema_version=3)
        passes[0]["role_card_sha256"] = "0" * 64

        with pytest.raises(SystemExit, match="manifest-role-card-hash-mismatch:qa-specialist"):
            _admit_manifest(tmp_path, manifest, passes)

    def test_preflight_rejects_spawned_pass_without_attempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A spawned pass with an empty attempt list fails the preflight before any result candidate exists.

        Routing, rollout lookup, and rollout reading are stubbed so only the attempt-cardinality check is exercised.
        """
        review_input = b"diff --git a/widget.py b/widget.py\n"
        (tmp_path / "diff.patch").write_bytes(review_input)
        (tmp_path / "specialists").mkdir()
        (tmp_path / "specialists" / "qa-specialist.md").write_text("QA evidence.\n", encoding="utf-8")
        _write_json(tmp_path / "review-routing.json", {"risk_tier": "LOCAL"})
        spawned_pass = {
            "role": "qa-specialist",
            "axis": "tests",
            "mode": "spawned",
            "trigger": "bug fix",
            "confidence": 0.9,
            "blocking_findings": 1,
            "output_path": "specialists/qa-specialist.md",
            "attempts": [],
            "selected_attempt": 1,
        }
        manifest = {
            "schema_version": 2,
            "review_run_id": "review-run",
            "parent_thread_id": "parent-thread",
            "review_input_sha256": hashlib.sha256(review_input).hexdigest(),
            "passes": [spawned_pass],
        }
        _write_json(tmp_path / "specialist-manifest.json", manifest)
        load_role_card = _VALIDATOR._load_role_card
        monkeypatch.setattr(_VALIDATOR, "_validate_routing", lambda _out, _tier: {"qa-specialist"})
        monkeypatch.setattr(_VALIDATOR, "_find_rollout", lambda _home, _thread: tmp_path / "rollout.jsonl")
        monkeypatch.setattr(_VALIDATOR, "_read_jsonl", lambda _path: [])
        monkeypatch.setattr(
            _VALIDATOR,
            "_load_role_card",
            lambda roles, role: {**load_role_card(roles, role), "model": "historical-model"},
        )

        with pytest.raises(SystemExit, match="manifest-invalid-attempt-count:qa-specialist"):
            _VALIDATOR._validate_manifest_preflight(tmp_path, tmp_path, "parent-thread", tmp_path)


class _ReviewRuntime(NamedTuple):
    """On-disk consumer-bound rollout evidence plus the review manifest that references it."""

    manifest: dict[str, Any]
    execution_path: Path
    out_dir: Path
    codex_home: Path
    nodes: list[dict[str, Any]]
    selected_passes: list[dict[str, Any]]
    review_manifest: dict[str, Any]

    def save_manifest(self) -> None:
        """Persist the execution manifest and rebind the review manifest to its new digest."""
        self.execution_path.write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
        self.review_manifest["runtime_execution"]["manifest_sha256"] = hashlib.sha256(
            self.execution_path.read_bytes()
        ).hexdigest()

    def validate(self) -> dict[str, Any]:
        """Validate the run through the shared review-runtime gate."""
        return _VALIDATOR._validate_review_runtime(
            self.out_dir, self.review_manifest, self.selected_passes, self.codex_home, "parent-thread"
        )


@pytest.fixture(name="real_schema_v3_review_runtime")
def _real_schema_v3_review_runtime(tmp_path: Path) -> _ReviewRuntime:
    """Build consumer-bound schema-three rollout evidence for review runtime checks."""
    fixtures = _parallel_execution_tests()
    run = fixtures._schema_v2_runtime_fixture(tmp_path)
    fixtures._bind_portable_read_consumer_policy(run, consumer_id="code-review")
    run.manifest["schema_version"] = 3
    plan = run.read_plan()
    parent_rows = [json.loads(line) for line in run.parent_rollout.read_text(encoding="utf-8").splitlines()]
    plan["dispatch_choices"] = {}
    for node in run.manifest["stages"][0]["nodes"]:
        attempt = node["attempts"][0]
        child_rollout = run.sessions_dir / f"rollout-{attempt['agent_thread_id']}.jsonl"
        child = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
        controls = next(row["payload"] for row in child if row["type"] == "turn_context")
        choice = {"role_id": node["role_id"], "model": controls["model"], "reasoning_effort": controls["effort"]}
        plan["dispatch_choices"][node["node_id"]] = choice
        spawn = next(
            row["payload"] for row in parent_rows if row.get("payload", {}).get("call_id") == attempt["spawn_call_id"]
        )
        arguments = json.loads(spawn["arguments"])
        arguments.update(model=choice["model"], reasoning_effort=choice["reasoning_effort"])
        spawn["arguments"] = json.dumps(arguments)
    run.save_plan(plan)
    fixtures._write_jsonl(run.parent_rollout, parent_rows)
    codex_home = tmp_path / "codex-home"
    copied_sessions = codex_home / "sessions"
    copied_sessions.mkdir(parents=True)
    for rollout in [*run.sessions_dir.glob("*.jsonl"), run.parent_rollout]:
        (copied_sessions / rollout.name).write_bytes(rollout.read_bytes())

    nodes = run.manifest["stages"][0]["nodes"]
    for node in nodes:
        role_card = PLUGIN_ROOT / "roles" / str(node["role_id"]) / "ROLE.md"
        node["role_card_sha256"] = hashlib.sha256(role_card.read_bytes()).hexdigest()
    selected_passes = [
        {
            "role": node["role_id"],
            "mode": "spawned",
            "output_path": node["attempts"][0]["output_path"],
            "attempts": [{"context_path": node["context_path"], "output_path": node["attempts"][0]["output_path"]}],
            "selected_attempt": 1,
        }
        for node in nodes
    ]
    review_manifest = {
        "runtime_execution": {
            "manifest_path": run.manifest_path.name,
            "manifest_sha256": "",
            "plan_path": run.plan_path.name,
        }
    }
    runtime = _ReviewRuntime(
        run.manifest, run.manifest_path, run.manifest_path.parent, codex_home, nodes, selected_passes, review_manifest
    )
    runtime.save_manifest()
    return runtime


class TestReviewRuntimeConsumer:
    """Review's consumer-bound portable runtime contract, checked through the shared validator."""

    @mock.patch.object(_VALIDATOR, "_find_rollout", autospec=True)
    @mock.patch.object(
        _VALIDATOR,
        "validate_read_only_runtime",
        autospec=True,
        return_value={
            "actual_mode": "serial",
            "evidence_level": "portable-read-restricted",
            "network_mode": "restricted",
            "approval_policy": "never",
            "filesystem_credential_isolation": "unverified",
            "runtime_promotion_eligible": True,
            "write_parallel_eligible": False,
            "consumer_id": "code-review",
        },
    )
    def test_binds_spawned_roles_to_shared_validator(
        self, validate_runtime: mock.MagicMock, find_rollout: mock.MagicMock, tmp_path: Path
    ) -> None:
        """A spawned role is checked by the shared runtime validator with this run's plan, rollout, and consumer id.

        Code review must not claim a spawned mode without the shared runtime gate; the exact inputs handed to that gate
        are the contract, so the validator itself is replaced and its call recorded.
        """
        plan_path = tmp_path / "execution-plan.json"
        plan_path.write_text('{"run_id":"review-run"}\n', encoding="utf-8")
        node = {
            "node_id": "qa",
            "role_id": "qa-specialist",
            "context_path": "specialists/qa-context.md",
            "attempts": [{"output_path": "specialists/qa.md"}],
            "selected_attempt": 1,
        }
        execution = {"schema_version": 1, "stages": [{"stage_id": "review", "nodes": [node]}]}
        execution_path = _write_json(tmp_path / "execution-manifest.json", execution)
        parent_rollout = tmp_path / "rollout-parent.jsonl"
        parent_rollout.write_text("{}\n", encoding="utf-8")
        find_rollout.return_value = parent_rollout
        pass_record = {
            "role": "qa-specialist",
            "mode": "spawned",
            "output_path": "specialists/qa.md",
            "attempts": [{"context_path": "specialists/qa-context.md", "output_path": "specialists/qa.md"}],
            "selected_attempt": 1,
        }
        manifest = {
            "runtime_execution": {
                "manifest_path": execution_path.name,
                "manifest_sha256": hashlib.sha256(execution_path.read_bytes()).hexdigest(),
                "plan_path": plan_path.name,
            }
        }

        summary = _VALIDATOR._validate_review_runtime(
            tmp_path, manifest, [pass_record], tmp_path / "codex-home", "parent-thread"
        )

        (payload,), keywords = validate_runtime.call_args
        expected_keywords = {
            "manifest_path": execution_path,
            "plan_path": plan_path,
            "parent_rollout": parent_rollout,
            "sessions_dir": tmp_path / "codex-home" / "sessions",
            "roles_dir": PLUGIN_ROOT / "roles",
            "expected_consumer_id": "code-review",
        }
        assert payload == execution
        assert expected_keywords.items() <= keywords.items()
        assert summary["actual_mode"] == "serial"
        assert summary["evidence_level"] == "portable-read-restricted"
        assert summary["network_mode"] == "restricted"
        assert summary["approval_policy"] == "never"
        assert "network_guarantee" not in summary

    def test_accepts_real_schema_v3_evidence(self, real_schema_v3_review_runtime: _ReviewRuntime) -> None:
        """Complete restricted-network schema-three rollout evidence is promoted by the shared validator.

        Unlike the mocked binding test, this runs the real validator over on-disk parent and child rollouts.
        """
        summary = real_schema_v3_review_runtime.validate()

        assert summary["evidence_level"] == "portable-read-restricted"
        assert summary["network_mode"] == "restricted"
        assert summary["approval_policy"] == "never"
        assert summary["filesystem_credential_isolation"] == "unverified"
        assert "network_guarantee" not in summary
        assert summary["write_parallel_eligible"] is False

    def test_rejects_legacy_control_tampering(self, real_schema_v3_review_runtime: _ReviewRuntime) -> None:
        """A legacy Boolean ``network: false`` is not accepted as a portable network restriction.

        The manifest digest is re-bound after tampering, so only the capability mismatch can reject the run.
        """
        runtime = real_schema_v3_review_runtime
        runtime.nodes[0]["observed_controls"]["network"] = False
        runtime.save_manifest()

        with pytest.raises(SystemExit, match="review-runtime-execution-invalid:runtime-node-capability-mismatch:N1"):
            runtime.validate()

    def test_rejects_spawn_without_manifest(self, tmp_path: Path) -> None:
        """A pass that records a child spawn needs authoritative runtime evidence; an empty manifest is rejected."""
        with pytest.raises(SystemExit, match="review-runtime-execution-missing"):
            _VALIDATOR._validate_review_runtime(
                tmp_path, {}, [{"role": "qa-specialist", "mode": "spawned"}], tmp_path, "parent-thread"
            )


class _PrSource(NamedTuple):
    """Routing, target, and checkout records that together describe an isolated exact-head PR checkout."""

    routing: dict[str, str]
    target: dict[str, str]
    checkout: dict[str, str]


def _python_import_proof(worktree: str) -> dict[str, object]:
    """Build one passing in-process test receipt bound to a tracked project module."""
    test_file = (Path(worktree) / "tests" / "test_widgets.py").as_posix()
    return {
        "mode": "in-process-pytest",
        "status": "pass",
        "invoked_interpreter": sys.executable,
        "runtime_interpreter": sys.executable,
        "sys_prefix": sys.prefix,
        "worktree": worktree,
        "tests": {test_file: {"origin": test_file, "tracked": True, "status": "pass", "reason": None}},
        "modules": {
            "widgets": {
                "origin": (Path(worktree) / "widgets" / "__init__.py").as_posix(),
                "tracked": True,
                "status": "pass",
                "reason": None,
            }
        },
    }


def _verified_review_worktree_source(tmp_path: Path, collection_name: str = "2026-01-01T00-00-00-000000Z") -> _PrSource:
    """Create exact-head checkout and gate receipts for isolated PR-source validation."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    if tmp_path.name == "run-001":
        collection_run = tmp_path.parent.parent / collection_name
    else:
        collection_run = tmp_path.with_name(collection_name)
    worktree = collection_run.with_name(f"{collection_run.name}-review-worktree").as_posix()
    routing = {"base_oid": BASE_OID, "head_oid": HEAD_OID, "checkout_method": "git-detached-review-worktree"}
    target = {"remote_ref": BASE_OID, "local_head": BASE_OID, "expected_base_oid": BASE_OID}
    checkout = {
        "expected_head": HEAD_OID,
        "local_head": HEAD_OID,
        "diff_base_oid": BASE_OID,
        "diff_head_oid": HEAD_OID,
        "collection_run_dir": collection_run.as_posix(),
        "worktree": worktree,
        "worktree_location": "report-adjacent",
        "source_worktree": tmp_path.parent.as_posix(),
        "local_branch": "",
        "worktree_lifecycle": "preserved-for-review",
    }
    clean_source = {
        "worktree": worktree,
        "expected_head": HEAD_OID,
        "before": {"head": HEAD_OID, "status": ""},
        "after": {"head": HEAD_OID, "status": ""},
    }
    payloads = {
        "pr.json": {"baseRefOid": BASE_OID, "headRefOid": HEAD_OID},
        "pr-head-fetch.json": {
            "remote_ref": "FETCH_HEAD",
            "local_head": HEAD_OID,
            "expected_head_oid": HEAD_OID,
            "head_matches_pr_metadata": True,
        },
        "worktree-preflight.json": {
            "phase": "after-checkout",
            "status": "already-at-pr-head",
            "worktree": worktree,
            "current_head": HEAD_OID,
            "expected_head": HEAD_OID,
            "dirty_paths": [],
            "unmerged_paths": [],
            "pr_paths": ["widget.py"],
            "checkout_paths": [],
            "overlapping_paths": [],
            "overlapping_pr_paths": [],
        },
        "source-worktree-context.json": {
            "phase": "source-worktree-context",
            "current_head": BASE_OID,
            "expected_head": HEAD_OID,
        },
        "checkout-state.json": {
            "status": "checkout-verified",
            "local_state": "isolated-exact-pr-head; main-worktree-unchanged",
            "local_head": HEAD_OID,
            "worktree": worktree,
        },
        "gates.json": {
            "source": {"worktree": worktree, "expected_head": HEAD_OID},
            "checks": [
                {
                    "id": "tests",
                    "status": "pass",
                    "source": clean_source,
                    "python_imports": _python_import_proof(worktree),
                },
                {
                    "id": "review",
                    "status": "pass",
                    "command_path": "checks/review.command.txt",
                    "source": clean_source,
                },
            ],
        },
    }
    for filename, payload in payloads.items():
        _write_json(tmp_path / filename, payload)
    (tmp_path / "checks").mkdir(exist_ok=True)
    (tmp_path / "checks" / "review.command.txt").write_text(
        f"git diff --check {BASE_OID}...{HEAD_OID}\n", encoding="utf-8"
    )
    return _PrSource(routing, target, checkout)


def _rebind_to_worktree(out: Path, worktree: str) -> None:
    """Point the preflight, checkout state, gate sources, and import proof of a staged PR source at ``worktree``."""
    for name in ("worktree-preflight.json", "checkout-state.json"):
        payload = read_json(out / name)
        payload["worktree"] = worktree
        _write_json(out / name, payload)
    gates = read_json(out / "gates.json")
    gates["source"]["worktree"] = worktree
    for check in gates["checks"]:
        check["source"]["worktree"] = worktree
    gates["checks"][0]["python_imports"] = _python_import_proof(worktree)
    _write_json(out / "gates.json", gates)


def _temporary_worktree_name(collection_run: str) -> str:
    """Return the deterministic temporary-fallback worktree name for one collection run."""
    digest = hashlib.sha256(collection_run.encode("utf-8")).hexdigest()[:16]
    return f"codex-pr-review-{digest}-review-worktree"


class TestVerifiedPrSource:
    """``_validate_verified_pr_source`` admits PR source only from an exact-head, clean, isolated checkout."""

    @pytest.mark.parametrize(
        "collection_name",
        [
            pytest.param("2026-01-01T00-00-00-000000Z", id="create-run-timestamp-name"),
            pytest.param("manual-review", id="arbitrary-collector-output-name"),
        ],
    )
    def test_accepts_isolated_review_worktree(self, tmp_path: Path, collection_name: str) -> None:
        """Exact PR source is accepted when the detached worktree and gate receipts agree.

        Direct collector output paths stay valid outside create-run naming conventions.
        """
        source = _verified_review_worktree_source(tmp_path, collection_name=collection_name)

        _VALIDATOR._validate_verified_pr_source(tmp_path, *source)

    def test_accepts_unavailable_invoking_worktree_context(self, tmp_path: Path) -> None:
        """An unavailable invoking-worktree context is informational and does not block isolated-source admission."""
        source = _verified_review_worktree_source(tmp_path)
        context_path = tmp_path / "source-worktree-context.json"
        context = read_json(context_path)
        unavailable = {
            "phase": context["phase"],
            "status": "unavailable",
            "current_head": context["current_head"],
            "expected_head": context["expected_head"],
            "reason": "command-output-oversized:source-worktree-context-untracked-worktree-paths",
        }
        _write_json(context_path, unavailable)

        _VALIDATOR._validate_verified_pr_source(tmp_path, *source)

    def test_accepts_original_worktree_after_run_promotion(self, tmp_path: Path) -> None:
        """A checkout created before PR run promotion stays bound to the moved report.

        Promotion renames ``.../<timestamp>`` to ``code-review/pr-123/run-001``; the recorded original run directory
        therefore differs from the promoted path, and that difference must not invalidate the checkout.
        """
        promoted = tmp_path / "code-review" / "pr-123" / "run-001"
        source = _verified_review_worktree_source(promoted)

        _VALIDATOR._validate_verified_pr_source(promoted, *source)

        assert source.checkout["collection_run_dir"] != promoted.as_posix()

    def test_accepts_temporary_review_worktree(self, tmp_path: Path) -> None:
        """The deterministic temporary fallback worktree is accepted while bound to the original collection run."""
        source = _verified_review_worktree_source(tmp_path)
        worktree = tmp_path.parent.parent / _temporary_worktree_name(source.checkout["collection_run_dir"])
        source.checkout["worktree"] = worktree.as_posix()
        source.checkout["worktree_location"] = "temporary-fallback"
        _rebind_to_worktree(tmp_path, worktree.as_posix())

        _VALIDATOR._validate_verified_pr_source(tmp_path, *source)

    @pytest.mark.parametrize("field", ["worktree", "collection_run_dir"])
    def test_rejects_checkout_record_pointing_elsewhere(self, tmp_path: Path, field: str) -> None:
        """A correct PR SHA cannot mask a checkout record whose worktree or original run is somewhere else."""
        source = _verified_review_worktree_source(tmp_path)
        source.checkout[field] = tmp_path.parent.joinpath("unrelated-worktree").as_posix()

        with pytest.raises(SystemExit, match=WORKTREE_MISMATCH):
            _VALIDATOR._validate_verified_pr_source(tmp_path, *source)

    @pytest.mark.parametrize(
        ("artifact", "retarget"),
        [
            pytest.param(
                "worktree-preflight.json", lambda payload, worktree: payload.update(worktree=worktree), id="preflight"
            ),
            pytest.param(
                "gates.json", lambda payload, worktree: payload["source"].update(worktree=worktree), id="gate-source"
            ),
            pytest.param(
                "gates.json",
                lambda payload, worktree: payload["checks"][0]["source"].update(worktree=worktree),
                id="check-source",
            ),
        ],
    )
    def test_rejects_artifact_pointing_at_another_worktree(
        self, tmp_path: Path, artifact: str, retarget: Callable[[dict[str, Any], str], object]
    ) -> None:
        """A correct PR SHA cannot mask tests or checkout evidence recorded in another worktree.

        The preflight, the gate-level source, and a single check's source each get retargeted in turn.
        """
        source = _verified_review_worktree_source(tmp_path)
        payload = read_json(tmp_path / artifact)
        retarget(payload, tmp_path.parent.joinpath("unrelated-worktree").as_posix())
        _write_json(tmp_path / artifact, payload)

        with pytest.raises(SystemExit, match=WORKTREE_MISMATCH):
            _VALIDATOR._validate_verified_pr_source(tmp_path, *source)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("head", "c" * 40, id="wrong-post-gate-head"),
            pytest.param("status", " M widget.py", id="dirty-post-gate-source"),
        ],
    )
    def test_rejects_passing_gate_without_clean_exact_head(self, tmp_path: Path, field: str, value: str) -> None:
        """A passing gate claim is rejected when its source receipt changed after execution."""
        source = _verified_review_worktree_source(tmp_path)
        gates = read_json(tmp_path / "gates.json")
        gates["checks"][0]["source"]["after"][field] = value
        _write_json(tmp_path / "gates.json", gates)

        with pytest.raises(SystemExit, match="pr-source-review-gates-source-invalid"):
            _VALIDATOR._validate_verified_pr_source(tmp_path, *source)

    def test_rejects_empty_worktree_diff_review_gate(self, tmp_path: Path) -> None:
        """A green review gate cannot be a bare ``git diff --check``, which inspects nothing in a clean worktree."""
        source = _verified_review_worktree_source(tmp_path)
        (tmp_path / "checks" / "review.command.txt").write_text("git diff --check\n", encoding="utf-8")

        with pytest.raises(SystemExit, match="pr-source-review-gate-command-invalid"):
            _VALIDATOR._validate_verified_pr_source(tmp_path, *source)

    def test_rejects_skipped_review_diff_gate(self, tmp_path: Path) -> None:
        """Committed-diff inspection is required even when a PR run labels the review gate not applicable."""
        source = _verified_review_worktree_source(tmp_path)
        gates = read_json(tmp_path / "gates.json")
        review = gates["checks"][1]
        review.update(status="not-applicable", reason="out of scope")
        review.pop("source")
        _write_json(tmp_path / "gates.json", gates)

        with pytest.raises(SystemExit, match="pr-source-review-gate-command-invalid"):
            _VALIDATOR._validate_verified_pr_source(tmp_path, *source)

    def test_retains_failed_review_diff_gate(self, tmp_path: Path) -> None:
        """A failed committed-diff check is admitted so it can reach a nonapproval review handoff."""
        source = _verified_review_worktree_source(tmp_path)
        gates = read_json(tmp_path / "gates.json")
        gates["checks"][1].update(status="fail", exit_code=2)
        _write_json(tmp_path / "gates.json", gates)

        _VALIDATOR._validate_verified_pr_source(tmp_path, *source)

    @pytest.mark.parametrize(
        "tamper",
        [
            pytest.param(lambda check, checkout: check.pop("python_imports"), id="free-form-pass"),
            pytest.param(
                lambda check, checkout: check["python_imports"].update(status="inconclusive"), id="inconclusive-proof"
            ),
            pytest.param(
                lambda check, checkout: check["python_imports"]["modules"]["widgets"].update(
                    origin=(Path(checkout["source_worktree"]) / "widgets" / "__init__.py").as_posix()
                ),
                id="main-worktree-origin",
            ),
            pytest.param(
                lambda check, checkout: check["python_imports"]["modules"]["widgets"].update(tracked=False),
                id="untracked-origin",
            ),
            pytest.param(lambda check, checkout: check["python_imports"].update(modules={}), id="empty-module-set"),
        ],
    )
    def test_rejects_unbound_passing_tests_imports(
        self, tmp_path: Path, tamper: Callable[[dict[str, Any], dict[str, str]], object]
    ) -> None:
        """A green tests gate needs real tracked project imports that resolve inside the PR worktree.

        A missing receipt, an inconclusive one, one resolving into the main worktree, an untracked origin, or an empty
        module set must each be rejected rather than certifying PR tests that never ran against PR source.
        """
        source = _verified_review_worktree_source(tmp_path)
        gates = read_json(tmp_path / "gates.json")
        tamper(gates["checks"][0], source.checkout)
        _write_json(tmp_path / "gates.json", gates)

        with pytest.raises(SystemExit, match="pr-source-review-tests-import-proof-invalid"):
            _VALIDATOR._validate_verified_pr_source(tmp_path, *source)

    @pytest.mark.parametrize(
        ("location", "locate_worktree"),
        [
            pytest.param(
                "report-adjacent", lambda run, tmp: run.with_name(f"{run.name}-review-worktree"), id="report-adjacent"
            ),
            pytest.param(
                "temporary-fallback",
                lambda run, tmp: tmp.parent / _temporary_worktree_name(run.as_posix()),
                id="temporary-fallback",
            ),
        ],
    )
    def test_promoted_assessed_review_accepts_bound_detached_worktree(
        self, tmp_path: Path, location: str, locate_worktree: Callable[[Path, Path], Path]
    ) -> None:
        """A completed, promoted PR review validates against the detached checkout made before promotion.

        The assessed review fixture is rebound to a worktree beside the pre-promotion collection run, or to the
        temporary fallback location, and full result validation must still accept it.
        """
        assessed = _completion_gate_tests()._assessed_pr.__wrapped__(tmp_path)
        routing = read_json(assessed / "pr-routing.json")
        checkout = read_json(assessed / "local-checkout.json")
        preflight = read_json(assessed / "worktree-preflight.json")
        gates = read_json(assessed / "gates.json")
        collection_run = assessed.parent.parent / "2026-01-01T00-00-00-000000Z"
        worktree = locate_worktree(collection_run, tmp_path)
        base_oid, head_oid = routing["base_oid"], routing["head_oid"]
        command = f"git worktree add --detach {worktree} {head_oid}"
        routing.update(
            local_checkout_command=command, checkout_mode="review", checkout_method="git-detached-review-worktree"
        )
        checkout.update(
            command=command,
            checkout_mode="review",
            checkout_method="git-detached-review-worktree",
            collection_run_dir=collection_run.as_posix(),
            worktree=worktree.as_posix(),
            worktree_location=location,
            source_worktree=tmp_path.as_posix(),
            worktree_lifecycle="preserved-for-review",
            local_branch="",
            diff_command=f"git -C {worktree} diff --binary {base_oid}...{head_oid} --",
        )
        preflight.update(worktree=worktree.as_posix(), status="already-at-pr-head")
        _bind_gates_to_worktree(gates, worktree.as_posix(), head_oid)
        _write_json(assessed / "pr-routing.json", routing)
        _write_json(assessed / "local-checkout.json", checkout)
        _write_json(assessed / "worktree-preflight.json", preflight)
        _write_json(assessed / "gates.json", gates)
        review_check = next(check for check in gates["checks"] if check["id"] == "review")
        (assessed / review_check["command_path"]).write_text(
            f"git diff --check {base_oid}...{head_oid}\n", encoding="utf-8"
        )
        context = {"phase": "source-worktree-context", "current_head": base_oid, "expected_head": head_oid}
        _write_json(assessed / "source-worktree-context.json", context)
        state = {
            "status": "checkout-verified",
            "local_state": "isolated-exact-pr-head; main-worktree-unchanged",
            "local_head": head_oid,
            "worktree": worktree.as_posix(),
        }
        _write_json(assessed / "checkout-state.json", state)

        _VALIDATOR._validate_result(assessed, assessed / "result.json", assessed, "thread", assessed)


def _bind_gates_to_worktree(gates: dict[str, Any], worktree: str, head_oid: str) -> None:
    """Bind the gate source and every executed check to a clean exact-head ``worktree`` with a passing import proof."""
    gates["source"] = {"worktree": worktree, "expected_head": head_oid}
    for check in gates["checks"]:
        if check["status"] not in {"not-applicable", "missing-command"}:
            check["source"] = {
                "worktree": worktree,
                "expected_head": head_oid,
                "before": {"head": head_oid, "status": ""},
                "after": {"head": head_oid, "status": ""},
            }
            if check["id"] == "tests" and check["status"] == "pass":
                check["python_imports"] = _python_import_proof(worktree)


def _stage_consolidation_inputs(out: Path, *, types_status: str = "pass", types_head: str = HEAD_OID) -> None:
    """Stage PR routing, checkout, and gate logs, adding a ``types`` gate with the given status and expected head."""
    source = _verified_review_worktree_source(out)
    artifacts = {
        "pr-routing.json": source.routing,
        "target-branch.json": source.target,
        "local-checkout.json": source.checkout,
        "review-routing.json": {"risk_tier": "HIGH_RISK", "sol_selection": {}},
    }
    for name, payload in artifacts.items():
        _write_json(out / name, payload)
    gates = read_json(out / "gates.json")
    types = {**gates["checks"][0], "id": "types", "status": types_status}
    types["source"] = {**types["source"], "expected_head": types_head}
    gates["checks"].append(types)
    for check in gates["checks"]:
        for key, suffix in (("command_path", "command"), ("stdout", "stdout"), ("stderr", "stderr")):
            check[key] = f"checks/{check['id']}.{suffix}.txt"
            path = out / check[key]
            if not path.exists():
                path.write_text("Mutation detected.\n" if suffix == "stdout" else "\n", encoding="utf-8")
    _write_json(out / "gates.json", gates)


def _gate_log_references(out: Path) -> Any:
    """Collect the gate-log references consolidation confidence admits for the staged run."""
    return _VALIDATOR._consolidation_confidence_evidence(out, {"batch_execution": {"waves": []}})


class TestConsolidationConfidence:
    """Consolidation admits mutation evidence only through verified PR checkout routing and passing gates."""

    @pytest.mark.parametrize(
        ("types_status", "types_log_listed"),
        [
            pytest.param("pass", True, id="passing-gate-listed"),
            pytest.param("fail", False, id="failed-gate-omitted"),
        ],
    )
    def test_lists_gate_logs_of_verified_pr_source(
        self, tmp_path: Path, types_status: str, types_log_listed: bool
    ) -> None:
        """Only a passing gate's stdout is admitted as mutation evidence; the review command log always is.

        A failing ``types`` gate keeps its command record but its mutation output must not count as confirmation.
        """
        _stage_consolidation_inputs(tmp_path, types_status=types_status)

        references = _gate_log_references(tmp_path)

        assert ("checks/types.stdout.txt" in references) is types_log_listed
        assert "checks/review.command.txt" in references

    def test_omits_gate_logs_without_pr_routing(self, tmp_path: Path) -> None:
        """A run without ``pr-routing.json`` is not a verified PR source, so none of its gate logs are admitted."""
        _stage_consolidation_inputs(tmp_path)
        (tmp_path / "pr-routing.json").unlink()

        references = _gate_log_references(tmp_path)

        assert "checks/types.stdout.txt" not in references
        assert "checks/review.command.txt" not in references

    def test_rejects_gate_recorded_for_another_head(self, tmp_path: Path) -> None:
        """A gate whose source names a different expected head fails the exact-source check."""
        _stage_consolidation_inputs(tmp_path, types_head="c" * 40)

        with pytest.raises(SystemExit, match="pr-source-review-gates-worktree-mismatch"):
            _gate_log_references(tmp_path)


def _historical_diff(range_spec: str) -> tuple[str, ...]:
    """Return the first-generation PR diff arguments."""
    return ("diff", "--binary", range_spec, "--")


def _driver_disabled_diff(range_spec: str) -> tuple[str, ...]:
    """Return PR diff arguments with external diff and textconv drivers disabled."""
    return ("diff", "--binary", "--no-ext-diff", "--no-textconv", range_spec, "--")


def _canonical_diff(range_spec: str) -> tuple[str, ...]:
    """Return the canonical PR diff arguments the collector issues today."""
    return _VALIDATOR.canonical_diff_arguments(_historical_diff(range_spec))


class TestPrDiffProvenance:
    """The recorded PR diff command must name the exact verified worktree and commit range."""

    @pytest.mark.parametrize(
        "diff_arguments",
        [
            pytest.param(_historical_diff, id="historical"),
            pytest.param(_driver_disabled_diff, id="drivers-disabled"),
            pytest.param(_canonical_diff, id="canonical"),
        ],
    )
    @pytest.mark.parametrize(
        ("coordinate", "command_path"),
        [
            pytest.param("C:/work/review", "C:/work/review", id="windows-drive-serialized"),
            pytest.param("C:/work/review", r"C:\work\review", id="windows-drive-native"),
            pytest.param("//server/share/review", "//server/share/review", id="windows-share-serialized"),
            pytest.param("//server/share/review", r"\\server\share\review", id="windows-share-native"),
            pytest.param("/work/review", "/work/review", id="posix"),
        ],
    )
    def test_preserves_serialized_and_native_coordinates(
        self, coordinate: str, command_path: str, diff_arguments: Callable[[str], tuple[str, ...]]
    ) -> None:
        """Both issued spellings of a worktree path are accepted, while a command for another worktree is rejected.

        The checkout coordinate is a cross-host string; the command may carry it serialized or in the issuing OS's
        native form, across each historical diff-argument generation, and ``<path>-other`` must never match.
        """
        command = " ".join(diff_arguments(f"{BASE_OID}...{HEAD_OID}"))
        checkout = {
            "worktree": coordinate,
            "diff_source": "verified-local-checkout",
            "diff_base_oid": BASE_OID,
            "diff_head_oid": HEAD_OID,
            "diff_command": f"git -C {command_path} {command}",
        }
        routing = {"checkout_method": "git-detached-review-worktree", "base_oid": BASE_OID, "head_oid": HEAD_OID}
        _VALIDATOR._validate_pr_diff_provenance(checkout, routing)
        checkout["diff_command"] = f"git -C {command_path}-other {command}"

        with pytest.raises(SystemExit, match="pr-local-diff-provenance-invalid"):
            _VALIDATOR._validate_pr_diff_provenance(checkout, routing)

    @pytest.mark.integration
    def test_plain_diff_check_misses_committed_whitespace(self, tmp_path: Path) -> None:
        """Only a base-to-head ``git diff --check`` catches trailing whitespace committed in a PR.

        In a real repository the whitespace is already committed, so the plain check on a clean tree passes while the
        three-dot range fails; this is why the detached worktree review gate needs the range form.
        """
        repository = tmp_path / "repository"
        repository.mkdir()

        def git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True, check=check)

        def commit(message: str) -> str:
            git("add", "widget.py")
            git(
                "-c",
                "user.name=Codex Test",
                "-c",
                "user.email=codex-test@example.invalid",
                "commit",
                "-q",
                "-m",
                f"{message}\n\nCo-authored-by: Codex <codex@openai.com>",
            )
            return git("rev-parse", "HEAD").stdout.decode().strip()

        git("init", "-q")
        source = repository / "widget.py"
        source.write_bytes(b"value = 1\n")
        base_oid = commit("Base")
        source.write_bytes(b"value = 1   \n")
        head_oid = commit("Whitespace")

        plain = git("diff", "--check", check=False)
        pr_diff = git("diff", "--check", f"{base_oid}...{head_oid}", check=False)

        assert plain.returncode == 0
        assert pr_diff.returncode != 0
        assert b"trailing whitespace" in pr_diff.stdout + pr_diff.stderr
