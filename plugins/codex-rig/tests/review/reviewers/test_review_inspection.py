"""Regression checks for supplied-context-only review inspection evidence."""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from _artifact_files import read_jsonl

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
ROLES = ("qa-specialist", "challenger")
UNKNOWN_CONTROLS = {role: {"sandbox_mode": "unknown", "approval_policy": "unknown"} for role in ROLES}
SCRIPT_PREAMBLE = "Script completed\nWall time 0.1 seconds\nOutput:\n"


@functools.cache
def _module(path: Path) -> ModuleType:
    """Load one test dependency without changing the import path; the module is loaded once per path."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validator() -> ModuleType:
    """Load the shipped validator without changing the test import path."""
    return _module(VALIDATOR_PATH)


def _reader() -> ModuleType:
    """Load the shipped frozen-context reader."""
    return _module(PLUGIN_ROOT / "skills/code-review/review_context.py")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """Write rollout-shaped evidence with portable newlines."""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def _parent_path(fixture: dict[str, object]) -> Path:
    """Return the parent rollout file of an inspection fixture."""
    return fixture["sessions"] / "sessions" / "rollout-parent-thread.jsonl"


def _child_path(fixture: dict[str, object], number: int = 1) -> Path:
    """Return the rollout file of the numbered child session of an inspection fixture."""
    return fixture["sessions"] / "sessions" / f"rollout-child-{number}.jsonl"


def _write_manifest(fixture: dict[str, object]) -> None:
    """Persist the fixture's possibly mutated manifest next to its run."""
    (fixture["run"] / "specialist-manifest.json").write_text(
        json.dumps(fixture["manifest"]), encoding="utf-8", newline="\n"
    )


def _preflight(fixture: dict[str, object]) -> None:
    """Run the shipped manifest preflight over an inspection fixture."""
    _validator()._validate_manifest_preflight(fixture["run"], fixture["sessions"], "parent-thread", fixture["run"])


def _runtime_summary(fixture: dict[str, object]) -> dict[str, object]:
    """Return the shipped runtime summary for an inspection fixture."""
    return _validator()._validate_review_runtime(
        fixture["run"], fixture["manifest"], fixture["passes"], fixture["sessions"], "parent-thread"
    )


def _inspection_run(
    tmp_path: Path,
    *,
    independent_required: bool = False,
    overlaps: bool = True,
    run_dir: Path | None = None,
    parent_thread_id: str = "parent-thread",
    source_rating_version: int | None = None,
) -> dict[str, object]:
    """Create a schema-five inspection run with real parent/child evidence relationships."""
    run = run_dir or tmp_path / "run"
    sessions = tmp_path / "codex-home" / "sessions"
    specialists = run / "specialists"
    specialists.mkdir(parents=True, exist_ok=True)
    sessions.mkdir(parents=True, exist_ok=True)
    review_input = (
        (run / "diff.patch").read_bytes() if (run / "diff.patch").exists() else b"diff --git a/widget.py b/widget.py\n"
    )
    (run / "diff.patch").write_bytes(review_input)
    input_hash = hashlib.sha256(review_input).hexdigest()
    plan = {
        "consumer_policy": {
            "consumer_id": "code-review",
            "capability": "instruction-bounded-review",
            "promotion_status": "promoted",
            "parent_mutations": "serial",
            "canonical_gates": "serial",
        },
        "review_operation": "inspection-only",
        "write_policy": {"parent_writes": "none", "approval_requirement": "not-required"},
        "review_run_id": "inspection-run",
        "parent_thread_id": parent_thread_id,
        "review_input_sha256": input_hash,
        "source_sensitivity": "non-sensitive",
        "model_selection": {"mode": "explicit", "model": "test-review-model"},
        "contexts": [],
        "independent_review_required": independent_required,
        "independence_requirement_evidence": "User explicitly required independent review."
        if independent_required
        else None,
    }
    plan_path = run / "inspection-plan.json"
    parent_rows: list[dict[str, object]] = [{"type": "session_meta", "payload": {"id": parent_thread_id}}]
    passes: list[dict[str, object]] = []
    for index, role in enumerate(ROLES, start=1):
        card_path = PLUGIN_ROOT / "roles" / role / "ROLE.md"
        card = card_path.read_text(encoding="utf-8")
        retained = run / "role-cards" / role / "ROLE.md"
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_bytes(card_path.read_bytes())
        fields = _validator()._load_role_card(PLUGIN_ROOT / "roles", role)
        context = f"{card}\nReview only the supplied source."
        context_path = specialists / f"{role}-context.md"
        context_path.write_text(context, encoding="utf-8", newline="\n")
        context_hash = hashlib.sha256(context_path.read_bytes()).hexdigest()
        plan["contexts"].append(
            {
                "role_id": role,
                "context_path": context_path.relative_to(run).as_posix(),
                "context_sha256": context_hash,
            }
        )
        message = (
            f"<!-- codex-review-provenance role={role} run=inspection-run input={input_hash} "
            f"context={context_hash} attempt=1 -->\nNo finding.\n\n"
            "## Reviewer Assessment\n\nRating: 1\nRationale: The inspected scope is clean."
        )
        if source_rating_version == 1:
            message = message.replace(
                "Rating: 1\n", "Source rating version: 1\nRating: 1\nVerification readiness: complete\n"
            )
        output_path = specialists / f"{role}.md"
        output_path.write_text(message + "\n", encoding="utf-8", newline="\n")
        thread = f"child-{index}"
        agent_path = f"/root/review_{role.replace('-', '_')}_{context_hash[:12]}_a1"
        turn = f"turn-{index}"
        event = f"event-{index}"
        started = 100 + index if overlaps else index * 100
        completed = 110 if overlaps else started + 10
        attempt = {
            "attempt": 1,
            "status": "completed",
            "agent_thread_id": thread,
            "event_id": event,
            "spawn_call_id": f"spawn-{index}",
            "agent_path": agent_path,
            "context_path": context_path.relative_to(run).as_posix(),
            "context_sha256": context_hash,
            "turn_id": turn,
            "model": "test-review-model",
            "effort": fields["model_reasoning_effort"],
            "output_path": output_path.relative_to(run).as_posix(),
            "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        }
        passes.append(
            {
                "role": role,
                "axis": "inspection",
                "mode": "inspection",
                "trigger": "bounded source inspection",
                "confidence": 0.95,
                "blocking_findings": 0,
                "output_path": attempt["output_path"],
                "role_card_sha256": fields["role_card_sha256"],
                "attempts": [attempt],
                "selected_attempt": 1,
            }
        )
        parent_rows.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "call_id": attempt["spawn_call_id"],
                        "arguments": json.dumps(
                            {
                                "message": context,
                                "task_name": Path(agent_path).name,
                                "fork_turns": "none",
                                "model": "test-review-model",
                            }
                        ),
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "started_at_ms": started * 1000,
                        "completed_at_ms": started * 1000 + 1,
                        "item": {
                            "type": "SubAgentActivity",
                            "id": event,
                            "kind": "started",
                            "agent_path": agent_path,
                            "agent_thread_id": thread,
                        },
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "author": agent_path,
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Message Type: FINAL_ANSWER\nPayload:\n{message}",
                            }
                        ],
                    },
                },
            ]
        )
        _write_jsonl(
            sessions / f"rollout-{thread}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread,
                        "agent_path": agent_path,
                        "agent_role": role,
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": parent_thread_id,
                                    "agent_path": agent_path,
                                    "agent_role": role,
                                }
                            }
                        },
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {
                        "turn_id": turn,
                        "model": "test-review-model",
                        "effort": fields["model_reasoning_effort"],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn,
                        "started_at": started,
                        "completed_at": completed,
                        "last_agent_message": message,
                    },
                },
            ],
        )
    plan_path.write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    _write_jsonl(sessions / "rollout-parent-thread.jsonl", parent_rows)
    manifest = {
        "schema_version": 5,
        "review_run_id": "inspection-run",
        "parent_thread_id": parent_thread_id,
        "review_input_sha256": input_hash,
        "passes": passes,
        "inspection_execution": {
            "plan_path": plan_path.name,
            "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
    }
    (run / "specialist-manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    routing = {
        "schema_version": 1,
        "risk_tier": "HIGH_RISK",
        "mechanical_risk_tier": "TRIVIAL",
        "mechanical_risk_evidence": ["files=0", "changed_lines=0", "unknown_size_rows=0"],
        "signals": {name: False for name in _validator().ROUTING_SIGNALS},
        "signal_evidence": {
            name: ["No additional axis in this supplied-context fixture."] for name in _validator().ROUTING_SIGNALS
        },
        "triggered_roles": sorted(ROLES),
        "trigger_reasons": {role: ["High-risk review axis."] for role in ROLES},
        "independent_review_required": independent_required,
        "independence_requirement_evidence": plan["independence_requirement_evidence"],
    }
    (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    _module(PLUGIN_ROOT / "skills/code-review/review_routing.py").synchronize_routing(run)
    return {"run": run, "sessions": sessions.parent, "manifest": manifest, "passes": passes, "plan": plan}


def _rewrite_inspection_plan(fixture: dict[str, object]) -> None:
    """Rebind one mutated frozen plan to its schema-five manifest."""
    plan_path = fixture["run"] / "inspection-plan.json"
    plan_path.write_text(json.dumps(fixture["plan"]), encoding="utf-8", newline="\n")
    fixture["manifest"]["inspection_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()


def _use_spawn_receipts(fixture: dict[str, object]) -> None:
    """Replace legacy activity with the native host's call-bound task-path receipts."""
    parent = _parent_path(fixture)
    rows = [row for row in read_jsonl(parent) if row["type"] != "event_msg"]
    for item in fixture["passes"]:
        attempt = item["attempts"][0]
        attempt.pop("event_id")
        call = next(row for row in rows if row["payload"].get("call_id") == attempt["spawn_call_id"])
        call["timestamp"] = "2026-01-01T10:00:00.000Z"
        child = fixture["sessions"] / "sessions" / f"rollout-{attempt['agent_thread_id']}.jsonl"
        child_rows = read_jsonl(child)
        child_rows[0]["payload"].update(
            parent_thread_id=fixture["manifest"]["parent_thread_id"], timestamp="2026-01-01T10:00:00.050Z"
        )
        _write_jsonl(child, child_rows)
        rows.append(
            {
                "timestamp": "2026-01-01T10:00:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": attempt["spawn_call_id"],
                    "output": json.dumps({"task_name": attempt["agent_path"]}),
                },
            }
        )
    _write_jsonl(parent, rows)
    _write_manifest(fixture)


def _use_frozen_read_receipts(fixture: dict[str, object]) -> None:
    """Model encrypted native dispatch and its one audited context read."""
    _use_spawn_receipts(fixture)
    fixture["manifest"]["schema_version"] = 6
    fixture["manifest"]["context_reader_python"] = sys.executable
    parent = _parent_path(fixture)
    parent_rows = read_jsonl(parent)
    validator = _validator()
    plan_path = fixture["run"] / "inspection-plan.json"
    for item in fixture["passes"]:
        role = item["role"]
        attempt = item["attempts"][0]
        ciphertext = f"encrypted-{role}"
        call = next(
            row
            for row in parent_rows
            if row["payload"].get("call_id") == attempt["spawn_call_id"]
            and row["payload"].get("type") == "function_call"
        )
        args = json.loads(call["payload"]["arguments"])
        args.update(
            message=ciphertext, agent_type="default", model=attempt["model"], reasoning_effort=attempt["effort"]
        )
        call["payload"]["arguments"] = json.dumps(args)
        child = fixture["sessions"] / "sessions" / f"rollout-{attempt['agent_thread_id']}.jsonl"
        child_rows = read_jsonl(child)
        child_rows[0]["payload"]["agent_role"] = "default"
        child_rows[0]["payload"]["source"]["subagent"]["thread_spawn"]["agent_role"] = "default"
        context = (fixture["run"] / attempt["context_path"]).read_text(encoding="utf-8")
        read_output = validator.render_read_output(
            context,
            role,
            fixture["manifest"]["review_run_id"],
            fixture["manifest"]["review_input_sha256"],
            attempt["context_sha256"],
            1,
        )
        child_rows.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "content": [{"type": "encrypted_content", "encrypted_content": ciphertext}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": f"read-{role}",
                        "input": validator.render_read_call(plan_path, role, 1, sys.executable, _include_workdir=True),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": f"read-{role}",
                        "output": [
                            {"type": "input_text", "text": SCRIPT_PREAMBLE},
                            {"type": "input_text", "text": read_output},
                        ],
                    },
                },
            ]
        )
        _write_jsonl(child, child_rows)
    _write_jsonl(parent, parent_rows)
    _write_manifest(fixture)


def _read_call_rows(
    reader: ModuleType, plan_path: Path, call_id: str, output: str, page: int
) -> list[dict[str, object]]:
    """Return the audited read call and its output row for one context page."""
    return [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": call_id,
                "input": reader.render_read_call(plan_path, ROLES[0], 1, sys.executable, page),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": call_id,
                "output": [{"type": "input_text", "text": SCRIPT_PREAMBLE}, {"type": "input_text", "text": output}],
            },
        },
    ]


def _paged_context(fixture: dict[str, object]) -> SimpleNamespace:
    """Rebind the first role to a multibyte context spanning several pages and build its in-order read rows."""
    reader = _reader()
    plan_path = fixture["run"] / "inspection-plan.json"
    context = "Review café 雪.\n" * 1100
    context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
    plan = fixture["plan"]
    manifest = {
        "context_reader_python": sys.executable,
        "review_run_id": plan["review_run_id"],
        "review_input_sha256": plan["review_input_sha256"],
    }
    pages = reader.context_pages(context)
    (fixture["run"] / plan["contexts"][0]["context_path"]).write_text(context, encoding="utf-8", newline="\n")
    plan["contexts"][0]["context_sha256"] = context_hash
    plan_path.write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    rendered = [
        reader.render_read_output(
            context, ROLES[0], manifest["review_run_id"], manifest["review_input_sha256"], context_hash, 1, page
        )
        for page in range(1, len(pages) + 1)
    ]
    rows = [
        row
        for page, output in enumerate(rendered, start=1)
        for row in _read_call_rows(reader, plan_path, f"read-{page}", output, page)
    ]
    return SimpleNamespace(
        reader=reader,
        plan_path=plan_path,
        context=context,
        pages=pages,
        rendered=rendered,
        rows=rows,
        attempt={"attempt": 1, "context_sha256": context_hash},
        manifest=manifest,
    )


@pytest.fixture
def inspection(tmp_path: Path) -> dict[str, object]:
    """Schema-five inspection run whose two reviewer children overlap in time and use legacy activity events."""
    return _inspection_run(tmp_path)


@pytest.fixture
def receipt_run(inspection: dict[str, object]) -> dict[str, object]:
    """Inspection run rewritten to the native host's call-bound spawn receipts."""
    _use_spawn_receipts(inspection)
    return inspection


@pytest.fixture
def frozen_read_run(inspection: dict[str, object]) -> dict[str, object]:
    """Schema-six inspection run with encrypted dispatch and one audited frozen-context read per child."""
    _use_frozen_read_receipts(inspection)
    return inspection


@pytest.fixture(params=[pytest.param(False, id="legacy-activity"), pytest.param(True, id="spawn-receipts")])
def host_run(request: pytest.FixtureRequest, inspection: dict[str, object]) -> tuple[dict[str, object], bool]:
    """Inspection run in each supported host evidence shape, paired with whether it uses spawn receipts."""
    if request.param:
        _use_spawn_receipts(inspection)
    return inspection, request.param


def _set_spawn_argument(rows: list[dict[str, object]], key: str, value: str) -> None:
    """Overwrite one argument of the first parent spawn call."""
    call = next(row for row in rows if row["payload"].get("type") == "function_call")
    arguments = json.loads(call["payload"]["arguments"])
    arguments[key] = value
    call["payload"]["arguments"] = json.dumps(arguments)


def _omit_second_page(rows: list[dict[str, object]], first_page_call: str) -> None:
    """Drop the read call and output of the second context page."""
    del rows[2:4]


def _reorder_pages(rows: list[dict[str, object]], first_page_call: str) -> None:
    """Swap the first two context pages."""
    rows[:4] = rows[2:4] + rows[:2]


def _truncate_second_output(rows: list[dict[str, object]], first_page_call: str) -> None:
    """Cut the last character from the second page's printed output."""
    rows[3]["payload"]["output"][1]["text"] = rows[3]["payload"]["output"][1]["text"][:-1]


def _repeat_last_page(rows: list[dict[str, object]], first_page_call: str) -> None:
    """Replay the final page's read call and output."""
    rows.extend(rows[-2:])


def _call_wrong_page(rows: list[dict[str, object]], first_page_call: str) -> None:
    """Make the second page's call request page one again."""
    rows[2]["payload"]["input"] = first_page_call


def _set_nested(payload: dict[str, object], keys: tuple[str, ...], value: str) -> None:
    """Set a value under a nested key path of a rollout payload."""
    for key in keys[:-1]:
        payload = payload[key]
    payload[keys[-1]] = value


def _append_child_row(fixture: dict[str, object], payload: dict[str, object]) -> None:
    """Append one extra response item to the first child session."""
    child = _child_path(fixture)
    _write_jsonl(child, [*read_jsonl(child), {"type": "response_item", "payload": payload}])


def _alter_parent_message(fixture: dict[str, object]) -> None:
    """Change the context text the parent sent in its first spawn call."""
    parent = _parent_path(fixture)
    rows = read_jsonl(parent)
    arguments = json.loads(rows[1]["payload"]["arguments"])
    arguments["message"] = "wrong"
    rows[1]["payload"]["arguments"] = json.dumps(arguments)
    _write_jsonl(parent, rows)


def _alter_child_parent(fixture: dict[str, object]) -> None:
    """Point the first child's thread-spawn lineage at a different parent."""
    child = _child_path(fixture)
    rows = read_jsonl(child)
    rows[0]["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = "other-parent"
    _write_jsonl(child, rows)


def _substitute_all_passes(fixture: dict[str, object]) -> None:
    """Turn every spawned pass into a parent substitute by dropping its attempts."""
    for item in fixture["passes"]:
        item.pop("attempts")
        item.pop("selected_attempt")
        item["mode"] = "substituted"


class TestInspectionFixture:
    """Protect the shared schema-five constructor that other suites reuse for inspection artifacts."""

    def test_initializes_a_patch_for_a_fresh_run(self, tmp_path: Path) -> None:
        """A supplied new run directory gets a default patch that the manifest hash binds.

        The constructor is reused by loop tests; the binding keeps their evidence internally consistent.
        """
        run = tmp_path / "explicit-run"
        run.mkdir()
        expected = b"diff --git a/widget.py b/widget.py\n"

        fixture = _inspection_run(tmp_path, run_dir=run)

        assert (run / "diff.patch").read_bytes() == expected
        assert fixture["manifest"]["review_input_sha256"] == hashlib.sha256(expected).hexdigest()

    @pytest.mark.parametrize(
        "existing",
        [pytest.param(b"", id="retained-empty-patch"), pytest.param(b"retained patch\n", id="retained-patch")],
    )
    def test_preserves_an_existing_patch(self, tmp_path: Path, existing: bytes) -> None:
        """An existing patch, even an empty one, stays byte-exact and the manifest hash binds to it."""
        run = tmp_path / "explicit-run"
        run.mkdir()
        (run / "diff.patch").write_bytes(existing)

        fixture = _inspection_run(tmp_path, run_dir=run)

        assert (run / "diff.patch").read_bytes() == existing
        assert fixture["manifest"]["review_input_sha256"] == hashlib.sha256(existing).hexdigest()


class TestFrozenContextRead:
    """Protect the schema-six frozen-context read that lets a child see source only through verified bytes."""

    def test_accepts_encrypted_dispatch_bound_to_frozen_read(self, frozen_read_run: dict[str, object]) -> None:
        """One child read validates even when the native spawn hides the dispatch plaintext.

        The host encrypts the dispatched message, so the only evidence of what the child saw is its audited read call.
        """
        _preflight(frozen_read_run)

    def test_accepts_code_fence_terminal_newline(self, frozen_read_run: dict[str, object]) -> None:
        """The single LF added when a child copies the printed JavaScript block does not fail the read binding."""
        child = _child_path(frozen_read_run)
        rows = read_jsonl(child)
        call = next(row for row in rows if row["payload"].get("type") == "custom_tool_call")
        call["payload"]["input"] += "\n"
        _write_jsonl(child, rows)

        _preflight(frozen_read_run)

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(
                lambda rows, encrypted, call, output: encrypted["payload"]["content"][0].update(
                    encrypted_content="different"
                ),
                id="encrypted",
            ),
            pytest.param(lambda rows, encrypted, call, output: rows.remove(call), id="missing-call"),
            pytest.param(
                lambda rows, encrypted, call, output: call["payload"].update(input="text('forged')"),
                id="wrong-call",
            ),
            pytest.param(lambda rows, encrypted, call, output: rows.append(call), id="extra-call"),
            pytest.param(lambda rows, encrypted, call, output: rows.remove(output), id="missing-output"),
            pytest.param(
                lambda rows, encrypted, call, output: output["payload"]["output"][1].update(text="forged"),
                id="wrong-output",
            ),
            pytest.param(lambda rows, encrypted, call, output: rows.append(output), id="extra-output"),
        ],
    )
    def test_rejects_broken_read(self, frozen_read_run: dict[str, object], mutate: Callable[..., object]) -> None:
        """Mismatched transport, an unobserved context, or an added child tool is rejected before promotion.

        Each case breaks one link of the encrypted-dispatch to audited-read chain in the first child's rollout.
        """
        child = _child_path(frozen_read_run)
        rows = read_jsonl(child)
        encrypted = next(row for row in rows if row["payload"].get("type") == "agent_message")
        call = next(row for row in rows if row["payload"].get("type") == "custom_tool_call")
        output = next(row for row in rows if row["payload"].get("type") == "custom_tool_call_output")
        mutate(rows, encrypted, call, output)
        _write_jsonl(child, rows)

        with pytest.raises(SystemExit, match=r"provenance-parent-spawn-mismatch|review-inspection-context-read"):
            _preflight(frozen_read_run)

    @pytest.mark.parametrize(
        ("payload", "error"),
        [
            pytest.param(
                {"type": "additional_tools", "role": "assistant", "tools": []},
                r"review-inspection-(child-tool-use|context-read-count-mismatch):qa-specialist",
                id="wrong-role",
            ),
            pytest.param(
                {"type": "function_call", "name": "exec"},
                r"review-inspection-(child-tool-use|context-read-count-mismatch):qa-specialist",
                id="actual-tool-call",
            ),
        ],
    )
    def test_rejects_executable_or_malformed_extra_activity(
        self, frozen_read_run: dict[str, object], payload: dict[str, object], error: str
    ) -> None:
        """Executable or malformed extra child activity is rejected next to an otherwise valid read."""
        child = _child_path(frozen_read_run)
        rows = read_jsonl(child)
        rows.insert(1, {"type": "response_item", "payload": payload})
        _write_jsonl(child, rows)

        with pytest.raises(SystemExit, match=error):
            _preflight(frozen_read_run)

    def test_accepts_host_tool_inventory(self, frozen_read_run: dict[str, object]) -> None:
        """A host-supplied tool inventory row is non-executing bookkeeping and does not break the read binding."""
        child = _child_path(frozen_read_run)
        inventory = {
            "type": "additional_tools",
            "id": "at-host",
            "role": "developer",
            "tools": [{"type": "namespace", "name": "functions", "tools": []}],
        }
        rows = read_jsonl(child)
        rows.insert(1, {"type": "response_item", "payload": inventory})
        _write_jsonl(child, rows)

        _preflight(frozen_read_run)

    def test_reader_returns_rendered_output_for_frozen_context(self, inspection: dict[str, object]) -> None:
        """The shipped reader prints exactly the rendered output of the frozen context bytes."""
        reader = _reader()
        plan = inspection["plan"]
        context = (inspection["run"] / plan["contexts"][0]["context_path"]).read_text(encoding="utf-8")

        output = reader.read_context(inspection["run"] / "inspection-plan.json", ROLES[0], 1)

        assert output == reader.render_read_output(
            context,
            ROLES[0],
            plan["review_run_id"],
            plan["review_input_sha256"],
            plan["contexts"][0]["context_sha256"],
            1,
        )

    def test_reader_rejects_changed_context(self, inspection: dict[str, object]) -> None:
        """A changed context file is refused before a child receives any source content.

        The plan binds each context by hash, so editing the frozen file after planning must fail the read.
        """
        context_path = inspection["run"] / inspection["plan"]["contexts"][0]["context_path"]
        context_path.write_text(context_path.read_text(encoding="utf-8") + "changed", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError, match="review-context-hash-mismatch"):
            _reader().read_context(inspection["run"] / "inspection-plan.json", ROLES[0], 1)


class TestPagedContext:
    """Protect schema-six paging: every UTF-8 page is read once, in order, even across multibyte boundaries."""

    def test_pages_cover_the_context_within_the_byte_limit(self, inspection: dict[str, object]) -> None:
        """A multibyte context splits into several pages that rejoin to the original and stay under 8 KiB each."""
        paged = _paged_context(inspection)

        assert len(paged.pages) > 1
        assert "".join(paged.pages) == paged.context
        assert all(len(page.encode("utf-8")) <= 8192 for page in paged.pages)

    def test_reader_returns_each_rendered_page(self, inspection: dict[str, object]) -> None:
        """Reading page N through the shipped reader returns exactly the rendered output for that page."""
        paged = _paged_context(inspection)

        outputs = [
            paged.reader.read_context(paged.plan_path, ROLES[0], 1, page) for page in range(1, len(paged.pages) + 1)
        ]

        assert outputs == paged.rendered

    def test_accepts_every_page_in_order(self, inspection: dict[str, object]) -> None:
        """A child that read every page once, in order, satisfies the context-read check."""
        paged = _paged_context(inspection)

        _validator()._validate_context_read(
            paged.rows, paged.plan_path, ROLES[0], paged.attempt, paged.manifest, paged.context
        )

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(_omit_second_page, id="omitted"),
            pytest.param(_reorder_pages, id="reordered"),
            pytest.param(_truncate_second_output, id="truncated"),
            pytest.param(_repeat_last_page, id="extra"),
            pytest.param(_call_wrong_page, id="wrong-page"),
        ],
    )
    def test_rejects_incomplete_or_disordered_pages(
        self, inspection: dict[str, object], mutate: Callable[[list[dict[str, object]], str], None]
    ) -> None:
        """A skipped, reordered, truncated, repeated, or mis-addressed page read fails the context-read check."""
        paged = _paged_context(inspection)
        first_page_call = paged.reader.render_read_call(paged.plan_path, ROLES[0], 1, sys.executable, 1)
        mutate(paged.rows, first_page_call)

        with pytest.raises(SystemExit, match="review-inspection-context-read"):
            _validator()._validate_context_read(
                paged.rows, paged.plan_path, ROLES[0], paged.attempt, paged.manifest, paged.context
            )


class TestSpawnReceipts:
    """Protect the binding of native call-bound spawn receipts to unique, correctly parented child sessions."""

    def test_accepts_call_bound_receipts(self, tmp_path: Path) -> None:
        """Current host receipts bind to unique child sessions without inventing activity ids.

        With independence explicitly required, the parallel children also satisfy it, and host controls the receipts
        never reveal are reported as unknown rather than assumed.
        """
        fixture = _inspection_run(tmp_path, independent_required=True)
        _use_spawn_receipts(fixture)

        _preflight(fixture)
        summary = _runtime_summary(fixture)

        assert summary["actual_mode"] == "parallel"
        assert summary["independence_satisfied"] is True
        assert summary["evidence_level"] == "instruction-bounded-review"
        assert summary["write_parallel_eligible"] is False
        assert summary["observed_controls"] == UNKNOWN_CONTROLS

    def test_ignores_legacy_non_thread_review_sessions(self, receipt_run: dict[str, object]) -> None:
        """Historical built-in review sessions in the same home do not invalidate unrelated native lineage."""
        _write_jsonl(
            receipt_run["sessions"] / "sessions" / "rollout-legacy.jsonl",
            [{"type": "session_meta", "payload": {"id": "legacy", "source": {"subagent": "review"}}}],
        )

        _preflight(receipt_run)

    @pytest.mark.parametrize(
        ("mutate", "error"),
        [
            pytest.param(
                lambda rows, receipt, fixture: rows.remove(receipt), "provenance-parent-spawn-mismatch", id="missing"
            ),
            pytest.param(
                lambda rows, receipt, fixture: receipt["payload"].update(call_id="unrelated-call"),
                "provenance-parent-spawn-mismatch",
                id="wrong-call",
            ),
            pytest.param(
                lambda rows, receipt, fixture: rows.append(receipt),
                "provenance-parent-spawn-mismatch",
                id="duplicate",
            ),
            pytest.param(
                lambda rows, receipt, fixture: receipt["payload"].update(
                    output=json.dumps({"task_name": "/root/unrelated"})
                ),
                "provenance-parent-spawn-mismatch",
                id="wrong-path",
            ),
            pytest.param(
                lambda rows, receipt, fixture: receipt["payload"].update(output="not-json"),
                "provenance-parent-spawn-mismatch",
                id="malformed",
            ),
            pytest.param(
                lambda rows, receipt, fixture: receipt["payload"].update(
                    output=json.dumps({"task_name": fixture["passes"][0]["attempts"][0]["agent_path"], "error": True})
                ),
                "provenance-parent-spawn-mismatch",
                id="extra-key",
            ),
            pytest.param(
                lambda rows, receipt, fixture: receipt.pop("timestamp"),
                "provenance-parent-spawn-mismatch",
                id="missing-time",
            ),
            pytest.param(
                lambda rows, receipt, fixture: receipt.update(timestamp="2026-01-01T10:00:00.100"),
                "provenance-parent-spawn-mismatch",
                id="naive-time",
            ),
            pytest.param(
                lambda rows, receipt, fixture: _set_spawn_argument(rows, "message", "wrong"),
                "provenance-parent-spawn-mismatch",
                id="wrong-context",
            ),
            pytest.param(
                lambda rows, receipt, fixture: _set_spawn_argument(rows, "fork_turns", "wrong"),
                "provenance-parent-spawn-mismatch",
                id="forked-context",
            ),
            pytest.param(
                lambda rows, receipt, fixture: rows.append(
                    next(row for row in rows if row["payload"].get("type") == "function_call")
                ),
                "review-inspection-spawn-missing",
                id="duplicate-call",
            ),
        ],
    )
    def test_rejects_unbound_parent_receipt(
        self, receipt_run: dict[str, object], mutate: Callable[..., object], error: str
    ) -> None:
        """A missing, duplicated, mis-addressed, or untimed parent receipt is never replaced by another call or path.

        Each case breaks the parent rollout's receipt or spawn call for the first child.
        """
        parent = _parent_path(receipt_run)
        rows = read_jsonl(parent)
        receipt = next(row for row in rows if row["payload"].get("type") == "function_call_output")
        mutate(rows, receipt, receipt_run)
        _write_jsonl(parent, rows)

        with pytest.raises(SystemExit, match=error):
            _preflight(receipt_run)

    @pytest.mark.parametrize(
        ("keys", "value"),
        [
            pytest.param(
                ("source", "subagent", "thread_spawn", "parent_thread_id"), "another-parent", id="wrong-parent"
            ),
            pytest.param(("source", "subagent", "thread_spawn", "agent_path"), "/root/wrong-child", id="spawn-path"),
            pytest.param(("timestamp",), "2025-01-01T10:00:00Z", id="stale-child"),
            pytest.param(("timestamp",), "2027-01-01T10:00:00Z", id="future-child"),
            pytest.param(("parent_thread_id",), "wrong", id="own-parent"),
            pytest.param(("agent_path",), "wrong", id="own-path"),
        ],
    )
    def test_rejects_unbound_child_header(
        self, receipt_run: dict[str, object], keys: tuple[str, ...], value: str
    ) -> None:
        """A child whose header names another parent, path, or spawn time cannot satisfy the receipt.

        The stale and future timestamps fall outside the window between the parent's call and its receipt.
        """
        child = _child_path(receipt_run)
        rows = read_jsonl(child)
        _set_nested(rows[0]["payload"], keys, value)
        _write_jsonl(child, rows)

        with pytest.raises(SystemExit, match="provenance-parent-spawn-mismatch"):
            _preflight(receipt_run)

    def test_rejects_ambiguous_child_session(self, receipt_run: dict[str, object]) -> None:
        """A second session claiming the first child's lineage makes the receipt ambiguous and is rejected."""
        child = _child_path(receipt_run)
        rows = read_jsonl(child)
        rows[0]["payload"]["id"] = "another-child"
        _write_jsonl(child.with_name("rollout-another-child.jsonl"), rows)

        with pytest.raises(SystemExit, match="provenance-parent-spawn-mismatch"):
            _preflight(receipt_run)

    def test_rejects_activity_event_missing_from_parent(self, receipt_run: dict[str, object]) -> None:
        """An attempt naming an activity event the parent never recorded cannot stand in for a receipt."""
        receipt_run["passes"][0]["attempts"][0]["event_id"] = "missing-event"
        _write_manifest(receipt_run)

        with pytest.raises(SystemExit, match="provenance-parent-spawn-mismatch"):
            _preflight(receipt_run)


class TestChildEvidence:
    """Protect schema-five acceptance of inspection children and rejection of tool use or unbound evidence."""

    def test_accepts_inspection_without_read_only_controls(self, inspection: dict[str, object]) -> None:
        """Source inspection is allowed while absent host controls are reported as unknown, not assumed."""
        _preflight(inspection)
        summary = _runtime_summary(inspection)

        assert summary["actual_mode"] == "parallel"
        assert summary["evidence_level"] == "instruction-bounded-review"
        assert summary["write_parallel_eligible"] is False
        assert summary["observed_controls"] == UNKNOWN_CONTROLS

    @pytest.mark.parametrize(
        ("schema", "mode"),
        [pytest.param(3, "inspection", id="no-legacy-inspection"), pytest.param(5, "spawned", id="no-mixed-route")],
    )
    def test_cannot_relabel_strict_evidence(self, inspection: dict[str, object], schema: int, mode: str) -> None:
        """Supplied-context review evidence stays out of strict runtime acceptance.

        An inspection pass under a legacy schema, or a strict spawned pass under the inspection schema, is a mismatched
        manifest and is rejected before runtime validation.
        """
        inspection["manifest"]["schema_version"] = schema
        inspection["passes"][0]["mode"] = mode
        _write_manifest(inspection)

        with pytest.raises(SystemExit, match="manifest-mode-schema-mismatch:qa-specialist"):
            _preflight(inspection)

    @pytest.mark.parametrize(
        ("tamper", "legacy_error", "receipt_error"),
        [
            pytest.param(
                lambda fixture: _append_child_row(fixture, {"type": "function_call", "name": "exec"}),
                "review-inspection-child-tool-use:qa-specialist",
                "review-inspection-child-tool-use:qa-specialist",
                id="tool",
            ),
            pytest.param(
                lambda fixture: _append_child_row(fixture, {"type": "function_call_output", "name": "exec"}),
                "review-inspection-child-tool-use:qa-specialist",
                "review-inspection-child-tool-use:qa-specialist",
                id="tool-output",
            ),
            pytest.param(
                _alter_parent_message,
                "review-inspection-context-not-sent:qa-specialist",
                "provenance-parent-spawn-mismatch",
                id="context",
            ),
            pytest.param(
                lambda fixture: (fixture["run"] / fixture["passes"][0]["attempts"][0]["output_path"]).write_text(
                    "changed\n", encoding="utf-8", newline="\n"
                ),
                "provenance-output-hash-mismatch:qa-specialist",
                "provenance-output-hash-mismatch:qa-specialist",
                id="output",
            ),
            pytest.param(
                _alter_child_parent,
                "provenance-child-parent-mismatch:child-1",
                "provenance-parent-spawn-mismatch",
                id="lineage",
            ),
        ],
    )
    def test_rejects_unbound_or_executing_child_evidence(
        self,
        host_run: tuple[dict[str, object], bool],
        tamper: Callable[[dict[str, object]], object],
        legacy_error: str,
        receipt_error: str,
    ) -> None:
        """Tool use and every required source, output, and lineage binding are rejected before promotion.

        Runs against both host evidence shapes; context and lineage tampering surfaces as a spawn mismatch once the
        parent carries call-bound receipts instead of legacy activity events.
        """
        fixture, spawn_receipts = host_run
        tamper(fixture)

        with pytest.raises(SystemExit, match=receipt_error if spawn_receipts else legacy_error):
            _preflight(fixture)

    @pytest.mark.parametrize(
        "event",
        [
            pytest.param({"type": "task_started"}, id="task-lifecycle"),
            pytest.param({"type": "token_count"}, id="token-accounting"),
            pytest.param({"type": "item_completed", "item": {"type": "Reasoning"}}, id="reasoning-completed"),
            pytest.param({"type": "item_completed", "item": {"type": "AgentMessage"}}, id="message-completed"),
            pytest.param({"type": "item_completed", "item": {"type": "ContextCompaction"}}, id="compaction-completed"),
        ],
    )
    def test_accepts_non_executing_host_events(self, inspection: dict[str, object], event: dict[str, object]) -> None:
        """Host lifecycle and bookkeeping events in a child rollout are not tool use."""
        child = _child_path(inspection)
        rows = read_jsonl(child)
        rows.insert(1, {"type": "event_msg", "payload": event})
        _write_jsonl(child, rows)

        _preflight(inspection)

    @pytest.mark.parametrize(
        "event",
        [
            pytest.param({"type": "item_completed", "item": {"type": "CommandExecution"}}, id="command-completed"),
            pytest.param({"type": "item_completed", "item": {"type": "FileChange"}}, id="file-change-completed"),
            pytest.param({"type": "item_completed", "item": {"type": "UnknownTool"}}, id="unknown-item"),
        ],
    )
    def test_rejects_completed_tool_activity(self, inspection: dict[str, object], event: dict[str, object]) -> None:
        """A completed command, file change, or unknown item in a child rollout counts as tool use."""
        child = _child_path(inspection)
        rows = read_jsonl(child)
        rows.insert(1, {"type": "event_msg", "payload": event})
        _write_jsonl(child, rows)

        with pytest.raises(SystemExit, match="review-inspection-child-tool-use:qa-specialist"):
            _preflight(inspection)


class TestRuntimeSummary:
    """Protect how execution mode and independence derive from observed child evidence."""

    def test_reports_serial_fallback_without_independence(self, inspection: dict[str, object]) -> None:
        """Substituted inspection output is admissible only as an honest non-independent fallback."""
        _substitute_all_passes(inspection)
        inspection["plan"]["contexts"] = []
        _rewrite_inspection_plan(inspection)

        summary = _runtime_summary(inspection)

        assert summary["actual_mode"] == "serial-fallback"
        assert summary["independence_satisfied"] is False
        assert summary["independence_required"] is False

    def test_sequential_children_are_not_parallel(self, tmp_path: Path) -> None:
        """The mode derives from terminal intervals rather than the dispatch shape.

        Two children that ran one after the other are independent but not parallel.
        """
        fixture = _inspection_run(tmp_path, overlaps=False)

        assert _runtime_summary(fixture)["actual_mode"] == "independent-spawned"

    def test_missing_required_role_lineage_is_not_independent(self, tmp_path: Path) -> None:
        """A substituted challenger cannot satisfy explicitly required independent role coverage."""
        fixture = _inspection_run(tmp_path, independent_required=True)
        fixture["passes"][-1].pop("attempts")
        fixture["passes"][-1].pop("selected_attempt")
        fixture["passes"][-1]["mode"] = "substituted"
        fixture["plan"]["contexts"].pop()
        _rewrite_inspection_plan(fixture)

        summary = _runtime_summary(fixture)

        assert summary["independence_required"] is True
        assert summary["independence_satisfied"] is False
        assert summary["actual_mode"] == "serial"

    def test_single_local_role_lineage_is_independent(self, inspection: dict[str, object]) -> None:
        """A lone QA inspection counts as independent coverage for its own local review axis."""
        inspection["passes"].pop()
        inspection["plan"]["contexts"].pop()
        _rewrite_inspection_plan(inspection)

        summary = _runtime_summary(inspection)

        assert summary["actual_mode"] == "serial"
        assert summary["independence_satisfied"] is True

    def test_empty_parent_only_route_is_not_independent(self, inspection: dict[str, object]) -> None:
        """A route with no triggered reviewer evidence stays outside independent coverage."""
        inspection["passes"].clear()
        inspection["plan"]["contexts"].clear()
        _rewrite_inspection_plan(inspection)

        assert _runtime_summary(inspection)["independence_satisfied"] is False

    def test_optional_axis_fallback_keeps_independence_but_not_pass_acceptance(
        self, inspection: dict[str, object]
    ) -> None:
        """A parent-only optional axis must not erase QA and challenger lineage, yet cannot be accepted as parallel.

        The runtime summary stays independent and parallel, but a passing review that triggered a third, substituted
        role still fails the parallel-review requirement.
        """
        inspection["passes"].append({"role": "doc-scribe", "mode": "substituted"})
        validator = _validator()
        summary = _runtime_summary(inspection)
        evidence = validator._SpecialistEvidence(
            triggered_roles={"qa-specialist", "challenger", "doc-scribe"},
            manifest=inspection["manifest"],
            routing=json.loads((inspection["run"] / "review-routing.json").read_text(encoding="utf-8")),
            passes=inspection["passes"],
            by_role={item["role"]: item for item in inspection["passes"]},
            runtime_summary=summary,
        )
        env = validator._ReviewEnvironment(
            codex_home=inspection["sessions"], parent_thread_id="parent-thread", project_root=inspection["run"]
        )

        with pytest.raises(SystemExit, match="parallel-review-required-for-pass:challenger,doc-scribe,qa-specialist"):
            validator._validate_independence_requirement(
                inspection["run"], evidence, {"independence_requirement_evidence": None}, "pass", "HIGH_RISK", env
            )

        assert summary["independence_satisfied"] is True
        assert summary["actual_mode"] == "parallel"


def _assessed_run(tmp_path: Path) -> Path:
    """Build a promoted assessed pull-request run through the completion-gate suite's own constructor."""
    completion = _module(Path(__file__).parents[1] / "validation" / "test_review_completion_gate.py")
    return completion._assessed_pr.__wrapped__(tmp_path)


def _reviewer_label(role: str) -> str:
    """Return the reviewer assessment label recorded for a specialist role."""
    return "QA specialist" if role == "qa-specialist" else "Challenger"


def _save_result(review: SimpleNamespace) -> None:
    """Persist the review's possibly mutated result next to its run."""
    review.result_path.write_text(json.dumps(review.result), encoding="utf-8", newline="\n")


def _validate_result(review: SimpleNamespace, path: Path | None = None) -> None:
    """Run the shipped result validation for an assessed multi-role review."""
    _validator()._validate_result(
        review.run, path or review.result_path, review.fixture["sessions"], "thread", review.run
    )


def _use_parent_substitutes(review: SimpleNamespace) -> None:
    """Rewrite both passes as parent substitutes with no spawned lineage, as an honest serial fallback."""
    fixture, run = review.fixture, review.run
    _substitute_all_passes(fixture)
    for item in fixture["passes"]:
        (run / item["output_path"]).write_text(
            f"role_id: {item['role']}\n\n## Reviewer Assessment\n\nSource rating version: 1\nRating: 3\n"
            "Verification readiness: complete\nRationale: Parent inspection lacks independent coverage.\n",
            encoding="utf-8",
            newline="\n",
        )
    fixture["plan"].update(independent_review_required=False, independence_requirement_evidence=None, contexts=[])
    _rewrite_inspection_plan(fixture)
    _write_manifest(fixture)
    review.metadata.update(
        specialist_passes=fixture["passes"],
        fanout_substituted=True,
        independence_required=False,
        independence_requirement_evidence=None,
        independence_satisfied=False,
        execution_mode="serial-fallback",
        execution_observed_controls={},
    )
    for assessment in review.metadata["reviewer_assessments"]:
        for item in fixture["passes"]:
            if assessment["evidence"] == item["output_path"]:
                assessment.update(role=_reviewer_label(item["role"]) + " (parent substitute)", rating=3)
    _save_result(review)


@pytest.fixture
def parallel_review(tmp_path: Path) -> SimpleNamespace:
    """Promoted assessed review run whose two reviewer passes ran in parallel under inspection evidence."""
    run = _assessed_run(tmp_path)
    fixture = _inspection_run(tmp_path, run_dir=run, parent_thread_id="thread", source_rating_version=1)
    result_path = run / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metadata = result["metadata"]
    metadata.update(
        risk_tier="HIGH_RISK",
        specialist_manifest=str(run / "specialist-manifest.json"),
        specialist_passes=fixture["passes"],
        review_run_id="inspection-run",
        review_input_sha256=fixture["manifest"]["review_input_sha256"],
        fanout_substituted=False,
        independence_required=False,
        independence_requirement_evidence=None,
        independence_satisfied=True,
        execution_mode="parallel",
        execution_evidence_level="instruction-bounded-review",
        write_parallel_eligible=False,
        execution_observed_controls=UNKNOWN_CONTROLS,
    )
    metadata["reviewer_assessments"].extend(
        {"role": _reviewer_label(item["role"]), "rating": 1, "evidence": item["output_path"]}
        for item in fixture["passes"]
    )
    review = SimpleNamespace(run=run, fixture=fixture, result=result, metadata=metadata, result_path=result_path)
    _save_result(review)
    return review


class TestMultiRoleResult:
    """Protect result validation for multi-role reviews: parallel evidence is required for a passing result."""

    def test_accepts_parallel_review(self, parallel_review: SimpleNamespace) -> None:
        """A passing result whose reviewer assessments bind to two parallel children validates."""
        _validate_result(parallel_review)

    def test_rejects_unbound_reviewer_role(self, parallel_review: SimpleNamespace) -> None:
        """A reviewer assessment naming a role that has no specialist pass is rejected."""
        candidate = json.loads(json.dumps(parallel_review.result))
        candidate["metadata"]["reviewer_assessments"] = [
            {"role": "Invented reviewer", "rating": 1, "evidence": parallel_review.fixture["passes"][0]["output_path"]}
        ]
        candidate_path = parallel_review.run / "result.candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8", newline="\n")

        with pytest.raises(SystemExit, match="review-assessment-role-unbound:Invented reviewer"):
            _validate_result(parallel_review, candidate_path)

    def test_rejects_sequential_children_labelled_independent(self, parallel_review: SimpleNamespace) -> None:
        """Children that finished one after the other cannot back a passing multi-role review."""
        second_child = _child_path(parallel_review.fixture, 2)
        child_rows = read_jsonl(second_child)
        child_rows[-1]["payload"].update(started_at=200, completed_at=210)
        _write_jsonl(second_child, child_rows)
        parallel_review.metadata["execution_mode"] = "independent-spawned"
        _save_result(parallel_review)

        with pytest.raises(SystemExit, match="parallel-review-required-for-pass:challenger,qa-specialist"):
            _validate_result(parallel_review)

    def test_rejects_passing_serial_fallback(self, parallel_review: SimpleNamespace) -> None:
        """A passing multi-role result built from parent substitutes lacks the required parallel review."""
        _use_parent_substitutes(parallel_review)

        with pytest.raises(SystemExit, match="parallel-review-required-for-pass:challenger,qa-specialist"):
            _validate_result(parallel_review)

    def test_accepts_honest_failed_serial_fallback(self, parallel_review: SimpleNamespace) -> None:
        """A failing result may honestly report a serial fallback, since no independence was demanded."""
        _use_parent_substitutes(parallel_review)
        validator = _validator()
        env = validator._ReviewEnvironment(
            codex_home=parallel_review.fixture["sessions"], parent_thread_id="thread", project_root=parallel_review.run
        )

        evidence = validator._validate_specialist_manifest(
            parallel_review.run,
            parallel_review.result,
            parallel_review.result_path,
            parallel_review.metadata,
            "HIGH_RISK",
            env,
        )
        validator._validate_independence_requirement(
            parallel_review.run, evidence, parallel_review.metadata, "fail", "HIGH_RISK", env
        )

    def test_rejects_serial_fallback_when_independence_required(self, parallel_review: SimpleNamespace) -> None:
        """A user-required independent review cannot pass on parent substitutes."""
        _use_parent_substitutes(parallel_review)
        required = "User explicitly required independent review."
        parallel_review.fixture["plan"].update(
            independent_review_required=True, independence_requirement_evidence=required
        )
        _rewrite_inspection_plan(parallel_review.fixture)
        _write_manifest(parallel_review.fixture)
        routing_path = parallel_review.run / "review-routing.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        routing.update(independent_review_required=True, independence_requirement_evidence=required)
        routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
        parallel_review.metadata.update(
            specialist_passes=parallel_review.fixture["passes"],
            independence_required=True,
            independence_requirement_evidence=required,
        )
        _save_result(parallel_review)

        with pytest.raises(SystemExit, match="independent-review-required-for-pass:challenger,qa-specialist"):
            _validate_result(parallel_review)


class TestAssessedResultRating:
    """Protect how an assessed result's reviewer rating binds to retained prose across schema versions."""

    def test_historical_result_keeps_original_rating_contract(self, tmp_path: Path) -> None:
        """A schema-two assessed result stays readable without the later prose rating section."""
        run = _assessed_run(tmp_path)
        result_path = run / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["schema_version"] = 2
        del result["metadata"]["reviewer_assessments"]
        result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")
        notes_path = run / "review-notes.md"
        notes = notes_path.read_text(encoding="utf-8")
        notes_path.write_text(notes.split("\n\n## Main Reviewer Assessment", 1)[0], encoding="utf-8", newline="\n")

        _validator()._validate_result(run, result_path, tmp_path / "codex-home", "thread", run)

    def test_current_result_requires_retained_rating_content(self, tmp_path: Path) -> None:
        """A schema-three rating must stay bound to the retained reviewer prose in the notes."""
        run = _assessed_run(tmp_path)
        notes_path = run / "review-notes.md"
        notes = notes_path.read_text(encoding="utf-8")
        notes_path.write_text(notes.split("\n\n## Main Reviewer Assessment", 1)[0], encoding="utf-8", newline="\n")

        with pytest.raises(SystemExit, match="review-assessment-content-invalid:main reviewer"):
            _validator()._validate_result(run, run / "result.json", tmp_path / "codex-home", "thread", run)
