"""Acceptance checks for truthful staged multi-agent execution evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile  # noqa: F401 - used by executable doctest examples
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PLUGIN_ROOT / "skills"
VALIDATOR_PATH = PLUGIN_ROOT / "shared" / "parallel_execution.py"


def _load_validator() -> ModuleType:
    """Load the standalone installed-package helper by file path."""
    specification = importlib.util.spec_from_file_location("codex_rig_parallel_execution", VALIDATOR_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    """Return the exact SHA-256 digest for one fixture file.

    Example:
        >>> len(_sha256(PLUGIN_ROOT / "package-manifest.json"))
        64
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_role(roles_dir: Path, role_id: str) -> str:
    """Write one canonical-card-shaped fixture and return its digest.

    Example:
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     len(_write_role(Path(directory), "qa-specialist"))
        64
    """
    path = roles_dir / role_id / "ROLE.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            (
                "---",
                f"role_id: {role_id}",
                f"name: codex-rig-{role_id}",
                "model: gpt-5.6-terra",
                "model_reasoning_effort: high",
                "approval_policy: on-request",
                "sandbox_mode: read-only",
                "---",
                "",
                f"# {role_id}",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )
    return _sha256(path)


def _event(event_id: str, sequence: int) -> dict[str, object]:
    """Return one parent-observed event fixture.

    Example:
        >>> _event("start", 1)
        {'event_id': 'start', 'sequence': 1}
    """
    return {"event_id": event_id, "sequence": sequence}


def _completed_node(
    run_dir: Path,
    roles_dir: Path,
    *,
    node_id: str,
    role_id: str,
    start: int,
    terminal: int,
    join: int,
) -> dict[str, object]:
    """Create one completed substantive read-only node with hash-bound evidence."""
    context_path = run_dir / f"{node_id}-context.md"
    output_path = run_dir / f"{node_id}-output.md"
    context_path.write_text(f"# Context for {node_id}\n", encoding="utf-8", newline="\n")
    output_path.write_text(f"Evidence from {node_id}.\n", encoding="utf-8", newline="\n")
    controls = {
        "sandbox_mode": "read-only",
        "write_paths": [],
        "network": False,
        "credentials": False,
    }
    return {
        "node_id": node_id,
        "role_id": role_id,
        "role_card_sha256": _write_role(roles_dir, role_id),
        "context_path": context_path.name,
        "context_sha256": _sha256(context_path),
        "mutation": "read-only",
        "owned_paths": [],
        "resource_locks": [],
        "requested_controls": controls,
        "observed_controls": {**controls, "enforced": True},
        "attempts": [
            {
                "attempt": 1,
                "status": "completed",
                "error_type": None,
                "start_event": _event(f"{node_id}-start", start),
                "terminal_event": _event(f"{node_id}-terminal", terminal),
                "output_path": output_path.name,
                "output_sha256": _sha256(output_path),
            }
        ],
        "selected_attempt": 1,
        "verifier_status": "passed",
        "unresolved": [],
        "join_event": _event(f"{node_id}-join", join),
    }


def _parallel_manifest(run_dir: Path, roles_dir: Path) -> dict[str, object]:
    """Return a valid two-node wave whose substantive intervals overlap."""
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "plan_sha256": "a" * 64,
        "claimed_mode": "parallel",
        "configured_limit": 4,
        "write_approval": None,
        "stages": [
            {
                "stage_id": "S1",
                "depends_on": [],
                "wave_id": "wave-alpha",
                "nodes": [
                    _completed_node(
                        run_dir,
                        roles_dir,
                        node_id="N1",
                        role_id="qa-specialist",
                        start=1,
                        terminal=4,
                        join=6,
                    ),
                    _completed_node(
                        run_dir,
                        roles_dir,
                        node_id="N2",
                        role_id="challenger",
                        start=2,
                        terminal=5,
                        join=7,
                    ),
                ],
            }
        ],
    }


def _validate(manifest: dict[str, object], run_dir: Path, roles_dir: Path) -> dict[str, object]:
    """Validate through the public installed-package function."""
    return _load_validator().validate_execution_manifest(manifest, run_dir=run_dir, roles_dir=roles_dir)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """Write one rollout-shaped fixture with portable newlines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def _runtime_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], Path, Path, Path, Path, Path]:
    """Create two host-bound read-only child records with overlapping work intervals."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    sessions_dir = tmp_path / "sessions"
    run_dir.mkdir()
    plan_path = run_dir / "execution-plan.json"
    plan = {
        "run_id": "run-1",
        "capability_policy": {"task_sensitivity": "non-sensitive"},
        "token_budgets": [
            {
                "wave_id": "wave-alpha",
                "ceiling_tokens": 1000,
                "node_order": ["N1", "N2"],
                "reservations": {"N1": 400, "N2": 400},
            }
        ],
    }
    plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8", newline="\n")
    manifest = _parallel_manifest(run_dir, roles_dir)
    manifest["plan_sha256"] = _sha256(plan_path)
    parent_thread_id = "parent-thread"
    parent_turn_id = "parent-turn"
    parent_rows: list[dict[str, object]] = [
        {
            "ordinal": 1,
            "timestamp": "2026-08-24T12:00:00Z",
            "type": "session_meta",
            "payload": {"id": parent_thread_id},
        }
    ]
    for index, node in enumerate(manifest["stages"][0]["nodes"], start=1):  # type: ignore[index]
        attempt = node["attempts"][0]
        thread_id = f"child-{index}"
        task_name = f"runtime_{node['node_id'].lower()}"
        agent_path = f"/root/{task_name}"
        turn_id = f"child-turn-{index}"
        call_id = f"spawn-call-{index}"
        start_id = f"activity-start-{index}"
        child_started = 1_800_000_000 + index
        child_completed = 1_800_000_010 - index
        attempt.update(
            {
                "agent_path": agent_path,
                "agent_thread_id": thread_id,
                "spawn_call_id": call_id,
                "turn_id": turn_id,
                "start_event": _event(start_id, 2 * index + 1),
                "terminal_event": _event(turn_id, 2 * index + 2),
            }
        )
        parent_rows.extend(
            [
                {
                    "ordinal": 2 * index,
                    "timestamp": "2026-08-24T12:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "call_id": call_id,
                        "arguments": json.dumps(
                            {
                                "agent_type": node["role_id"],
                                "fork_turns": "3",
                                "message": f"private context for {node['node_id']}",
                                "task_name": task_name,
                            }
                        ),
                    },
                },
                {
                    "ordinal": 2 * index + 1,
                    "timestamp": "2026-08-24T12:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "thread_id": parent_thread_id,
                        "turn_id": parent_turn_id,
                        "started_at_ms": child_started * 1000,
                        "completed_at_ms": child_started * 1000 + 100,
                        "item": {
                            "type": "SubAgentActivity",
                            "id": start_id,
                            "kind": "started",
                            "agent_path": agent_path,
                            "agent_thread_id": thread_id,
                        },
                    },
                },
            ]
        )
        output_path = run_dir / str(attempt["output_path"])
        message = output_path.read_text(encoding="utf-8").strip()
        parent_rows.append(
            {
                "ordinal": 10 + index,
                "timestamp": "2027-01-15T09:00:20Z",
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "id": node["join_event"]["event_id"],
                    "author": agent_path,
                    "recipient": "/root",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "\n".join(
                                (
                                    "Message Type: FINAL_ANSWER",
                                    "Task name: /root",
                                    f"Sender: {agent_path}",
                                    "Payload:",
                                    message,
                                )
                            ),
                        }
                    ],
                },
            }
        )
        _write_jsonl(
            sessions_dir / f"rollout-{thread_id}.jsonl",
            [
                {
                    "ordinal": 1,
                    "timestamp": "2026-08-24T12:00:01Z",
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "agent_path": agent_path,
                        "agent_role": node["role_id"],
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": parent_thread_id,
                                    "agent_path": agent_path,
                                    "agent_role": node["role_id"],
                                }
                            }
                        },
                    },
                },
                {
                    "ordinal": 2,
                    "timestamp": "2026-08-24T12:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": {
                            "model": "gpt-5.6-terra",
                            "reasoning_effort": "high",
                            "approval_policy": "on-request",
                            "permission_profile": {
                                "type": "managed",
                                "file_system": {"type": "restricted", "entries": []},
                                "network": False,
                            },
                        },
                    },
                },
                {
                    "ordinal": 3,
                    "timestamp": "2026-08-24T12:00:01Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": turn_id,
                        "model": "gpt-5.6-terra",
                        "effort": "high",
                        "approval_policy": "on-request",
                        "sandbox_policy": {"type": "read-only"},
                        "permission_profile": {"type": "managed", "network": False},
                    },
                },
                {
                    "ordinal": 4,
                    "timestamp": "2026-08-24T12:00:10Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn_id,
                        "started_at": child_started,
                        "completed_at": child_completed,
                        "duration_ms": (child_completed - child_started) * 1000,
                        "last_agent_message": message,
                    },
                },
            ],
        )
    parent_rollout = sessions_dir / "rollout-parent-thread.jsonl"
    _write_jsonl(parent_rollout, parent_rows)
    manifest_path = run_dir / "execution-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir


def _validate_runtime(
    manifest: dict[str, object],
    manifest_path: Path,
    plan_path: Path,
    parent_rollout: Path,
    sessions_dir: Path,
    roles_dir: Path,
    *,
    historical_unbudgeted: bool = False,
    expected_consumer_id: str | None = None,
) -> dict[str, object]:
    """Validate one read-only run against rollout-shaped host evidence."""
    return _load_validator().validate_read_only_runtime(
        manifest,
        manifest_path=manifest_path,
        plan_path=plan_path,
        parent_rollout=parent_rollout,
        sessions_dir=sessions_dir,
        run_dir=manifest_path.parent,
        roles_dir=roles_dir,
        historical_unbudgeted=historical_unbudgeted,
        expected_consumer_id=expected_consumer_id,
    )


def _schema_v2_runtime_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], Path, Path, Path, Path, Path]:
    """Create a portable schema-v2 run with restricted, approval-free networking."""
    fixture = _runtime_fixture(tmp_path)
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = fixture
    manifest["schema_version"] = 2
    manifest["capability_evidence"] = {
        "tier": "portable",
        "task_sensitivity": "non-sensitive",
        "network": {
            "mode": "restricted",
            "approval_policy": "never",
            "external_events": [],
        },
        "credentials": {
            "context_scan": "passed",
            "filesystem_isolation": "unverified",
        },
    }
    for node in manifest["stages"][0]["nodes"]:  # type: ignore[index]
        controls = {
            "sandbox_mode": "read-only",
            "write_paths": [],
            "network": "restricted",
            "credentials": "unverified",
        }
        node["requested_controls"] = controls
        node["observed_controls"] = {**controls, "enforced": True}
        attempt = node["attempts"][0]
        child_rollout = sessions_dir / f"rollout-{attempt['agent_thread_id']}.jsonl"
        rows = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            payload = row.get("payload", {})
            if row.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
                payload["thread_settings"]["approval_policy"] = "never"
                payload["thread_settings"]["permission_profile"]["network"] = "restricted"
            elif row.get("type") == "turn_context":
                payload["approval_policy"] = "never"
                payload["permission_profile"]["network"] = "restricted"
        _write_jsonl(child_rollout, rows)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return fixture


def _bind_portable_read_consumer_policy(
    manifest: dict[str, object],
    manifest_path: Path,
    plan_path: Path,
    *,
    consumer_id: str,
    parent_writes: str = "none",
) -> None:
    """Bind a fixture's frozen plan to one promoted portable read-only consumer."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["consumer_policy"] = {
        "consumer_id": consumer_id,
        "capability": "portable-read-only",
        "promotion_status": "promoted",
        "parent_mutations": "serial",
        "canonical_gates": "serial",
    }
    plan["write_policy"] = {
        "parent_writes": parent_writes,
        "approval_requirement": "exact-plan-digest" if parent_writes == "planned" else "not-required",
    }
    plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8", newline="\n")
    manifest["plan_sha256"] = _sha256(plan_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


def test_execution_mode_precedence_and_shipped_default() -> None:
    """Keep explicit intent above ambient configuration and default to safe auto."""
    resolver = _load_validator().resolve_execution_mode

    assert resolver(
        "--execution=serial",
        environment={"CODEX_RIG_EXECUTION": "parallel-read"},
        read_parallel_promoted=True,
        write_parallel_promoted=False,
    ) == {
        "effective_mode": "serial",
        "requested_mode": "serial",
        "source": "explicit",
        "write_approval_required": False,
    }
    assert (
        resolver(
            None,
            environment={"CODEX_RIG_EXECUTION": "parallel-read"},
            read_parallel_promoted=True,
            write_parallel_promoted=False,
        )["effective_mode"]
        == "parallel-read"
    )
    assert resolver(
        None,
        environment={},
        read_parallel_promoted=False,
        write_parallel_promoted=False,
    ) == {
        "effective_mode": "serial",
        "requested_mode": "auto",
        "source": "shipped-default",
        "write_approval_required": False,
    }
    assert (
        resolver(
            None,
            environment={},
            read_parallel_promoted=True,
            write_parallel_promoted=True,
        )["effective_mode"]
        == "parallel-read"
    )


def test_auto_default_never_selects_an_unpromoted_or_write_only_capability() -> None:
    """Keep the auto default serial unless the portable read route is promoted."""
    resolver = _load_validator().resolve_execution_mode

    for read_parallel_promoted, write_parallel_promoted in ((False, False), (False, True)):
        resolution = resolver(
            None,
            environment={},
            read_parallel_promoted=read_parallel_promoted,
            write_parallel_promoted=write_parallel_promoted,
        )

        assert resolution["requested_mode"] == "auto"
        assert resolution["effective_mode"] == "serial"
        assert resolution["write_approval_required"] is False


@pytest.mark.parametrize("mode", ["parallel-read", "parallel-write"])
def test_execution_mode_rejects_unpromoted_parallel_requests(mode: str) -> None:
    """Prevent a flag or environment value from bypassing consumer promotion."""
    resolver = _load_validator().resolve_execution_mode

    with pytest.raises(ValueError, match=f"^{mode}-not-promoted$"):
        resolver(
            f"--execution={mode}",
            environment={},
            read_parallel_promoted=False,
            write_parallel_promoted=False,
        )


def test_auto_before_read_promotion_resolves_to_serial() -> None:
    """Keep auto safe before the portable read route is promoted."""
    resolution = _load_validator().resolve_execution_mode(
        "--execution=auto",
        environment={},
        read_parallel_promoted=False,
        write_parallel_promoted=False,
    )

    assert resolution["requested_mode"] == "auto"
    assert resolution["effective_mode"] == "serial"


def test_parallel_write_request_never_carries_approval() -> None:
    """Keep mode selection separate from the mandatory digest-bound approval."""
    resolution = _load_validator().resolve_execution_mode(
        "--execution=parallel-write",
        environment={"CODEX_RIG_EXECUTION": "auto"},
        read_parallel_promoted=True,
        write_parallel_promoted=True,
    )

    assert resolution == {
        "effective_mode": "parallel-write",
        "requested_mode": "parallel-write",
        "source": "explicit",
        "write_approval_required": True,
    }


def test_structural_overlap_derives_parallel_only_from_substantive_joined_nodes(tmp_path: Path) -> None:
    """Prevent spawn-only or sequential fan-out from masquerading as parallel work."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)

    summary = _validate(manifest, run_dir, roles_dir)

    assert summary == {
        "acceptance_blocked": False,
        "actual_mode": "parallel",
        "integration_order": ["N1", "N2"],
    }


def test_sequential_intervals_cannot_claim_parallel(tmp_path: Path) -> None:
    """Reject a truthful-label violation even when both children completed."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    second = manifest["stages"][0]["nodes"][1]  # type: ignore[index]
    second["attempts"][0]["start_event"] = _event("N2-start", 5)  # type: ignore[index]
    second["attempts"][0]["terminal_event"] = _event("N2-terminal", 6)  # type: ignore[index]
    second["join_event"] = _event("N2-join", 7)  # type: ignore[index]

    with pytest.raises(ValueError, match="^false-parallel-claim$"):
        _validate(manifest, run_dir, roles_dir)


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        pytest.param("role", "role-card-hash-mismatch:N1", id="role"),
        pytest.param("context", "context-hash-mismatch:N1", id="context"),
        pytest.param("output", "output-hash-mismatch:N1", id="output"),
        pytest.param("join", "join-before-terminal:N1", id="join"),
    ),
)
def test_hash_and_join_provenance_fail_closed(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    """Bind acceptance to exact role, context, output, and parent join evidence."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    if mutation == "role":
        first["role_card_sha256"] = "0" * 64  # type: ignore[index]
    elif mutation == "context":
        first["context_sha256"] = "0" * 64  # type: ignore[index]
    elif mutation == "output":
        first["attempts"][0]["output_sha256"] = "0" * 64  # type: ignore[index]
    else:
        first["join_event"] = _event("N1-join", 4)  # type: ignore[index]

    with pytest.raises(ValueError, match=f"^{error}$"):
        _validate(manifest, run_dir, roles_dir)


def test_stage_dependencies_must_form_a_complete_dag(tmp_path: Path) -> None:
    """Prevent a missing or cyclic stage from starting outside its barrier."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    manifest["stages"][0]["depends_on"] = ["missing"]  # type: ignore[index]

    with pytest.raises(ValueError, match="^stage-dependency-missing:S1$"):
        _validate(manifest, run_dir, roles_dir)


def test_independent_stages_cannot_bypass_the_serial_barrier(tmp_path: Path) -> None:
    """Prevent cross-stage overlap from escaping ownership, lock, and concurrency checks."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    manifest["stages"].append(  # type: ignore[union-attr]
        {
            "stage_id": "S2",
            "depends_on": [],
            "wave_id": "wave-beta",
            "nodes": [
                _completed_node(
                    run_dir,
                    roles_dir,
                    node_id="N3",
                    role_id="qa-specialist-secondary",
                    start=2,
                    terminal=5,
                    join=8,
                )
            ],
        }
    )

    with pytest.raises(ValueError, match="^stage-barrier-required:S2$"):
        _validate(manifest, run_dir, roles_dir)


def test_wave_identifiers_are_unique_across_serial_stages(tmp_path: Path) -> None:
    """Keep event and join evidence attributable to exactly one staged wave."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    manifest["stages"].append(  # type: ignore[union-attr]
        {
            "stage_id": "S2",
            "depends_on": ["S1"],
            "wave_id": "wave-alpha",
            "nodes": [
                _completed_node(
                    run_dir,
                    roles_dir,
                    node_id="N3",
                    role_id="qa-specialist-secondary",
                    start=8,
                    terminal=9,
                    join=10,
                )
            ],
        }
    )

    with pytest.raises(ValueError, match="^wave-id-duplicate:wave-alpha$"):
        _validate(manifest, run_dir, roles_dir)


def test_configured_limit_cannot_exceed_the_default_ceiling(tmp_path: Path) -> None:
    """Keep a local configuration from silently exceeding the four-child safety ceiling."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    manifest["configured_limit"] = 5

    with pytest.raises(ValueError, match="^configured-limit-invalid$"):
        _validate(manifest, run_dir, roles_dir)


def test_parallel_write_paths_and_resources_cannot_overlap(tmp_path: Path) -> None:
    """Prevent two write owners from racing on an alias, ancestor, or shared lock."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    manifest["write_approval"] = {
        "plan_sha256": manifest["plan_sha256"],
        "response": "approve",
        "source": "explicit-input",
    }
    for node in manifest["stages"][0]["nodes"]:  # type: ignore[index]
        node["mutation"] = "write"
        node["owned_paths"] = ["src/shared.py"]
        node["resource_locks"] = ["git-index"]
        controls = {
            "sandbox_mode": "workspace-write",
            "write_paths": ["src/shared.py"],
            "network": False,
            "credentials": False,
        }
        node["requested_controls"] = controls
        node["observed_controls"] = {**controls, "enforced": True}

    with pytest.raises(ValueError, match="^write-ownership-overlap:N1:N2$"):
        _validate(manifest, run_dir, roles_dir)


def test_windows_and_posix_owned_path_aliases_overlap(tmp_path: Path) -> None:
    """Prevent native-Windows separators from bypassing write ownership isolation."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    manifest["write_approval"] = {
        "plan_sha256": manifest["plan_sha256"],
        "response": "approve",
        "source": "explicit-input",
    }
    for node, path in zip(
        manifest["stages"][0]["nodes"],  # type: ignore[index]
        ("src\\shared.py", "src/shared.py"),
        strict=True,
    ):
        node["mutation"] = "write"
        node["owned_paths"] = [path]
        controls = {
            "sandbox_mode": "workspace-write",
            "write_paths": [path],
            "network": False,
            "credentials": False,
        }
        node["requested_controls"] = controls
        node["observed_controls"] = {**controls, "enforced": True}

    with pytest.raises(ValueError, match="^write-ownership-overlap:N1:N2$"):
        _validate(manifest, run_dir, roles_dir)


def test_resource_locks_use_the_validated_vocabulary(tmp_path: Path) -> None:
    """Reject an unscoped custom lock that cannot be compared consistently."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    first["resource_locks"] = ["custom-lock"]  # type: ignore[index]

    with pytest.raises(ValueError, match="^resource-lock-invalid:N1$"):
        _validate(manifest, run_dir, roles_dir)


def test_every_serial_write_requires_exact_frozen_plan_approval(tmp_path: Path) -> None:
    """Prevent serial execution from bypassing the universal write-approval gate."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    manifest["claimed_mode"] = "serial"
    manifest["stages"][0]["nodes"] = manifest["stages"][0]["nodes"][:1]  # type: ignore[index]
    node = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    node["mutation"] = "write"
    node["owned_paths"] = ["src/feature.py"]
    controls = {
        "sandbox_mode": "workspace-write",
        "write_paths": ["src/feature.py"],
        "network": False,
        "credentials": False,
    }
    node["requested_controls"] = controls
    node["observed_controls"] = {**controls, "enforced": True}

    with pytest.raises(ValueError, match="^write-approval-required$"):
        _validate(manifest, run_dir, roles_dir)

    valid_approval = {
        "plan_sha256": manifest["plan_sha256"],
        "response": "approve",
        "source": "explicit-input",
    }
    for field, value in (
        ("plan_sha256", "0" * 64),
        ("response", "deny"),
        ("source", "environment"),
    ):
        manifest["write_approval"] = {**valid_approval, field: value}
        with pytest.raises(ValueError, match="^write-approval-invalid$"):
            _validate(manifest, run_dir, roles_dir)

    manifest["write_approval"] = valid_approval

    assert _validate(manifest, run_dir, roles_dir)["actual_mode"] == "serial"


def test_unproved_node_controls_block_parallel_acceptance(tmp_path: Path) -> None:
    """Prevent requested-only sandbox claims from satisfying least privilege."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    first["observed_controls"]["enforced"] = False  # type: ignore[index]

    with pytest.raises(ValueError, match="^capability-enforcement-required:N1$"):
        _validate(manifest, run_dir, roles_dir)


def test_common_secret_material_is_rejected_from_context_packs(tmp_path: Path) -> None:
    """Prevent a hash-valid context pack from persisting obvious credential material."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    context = run_dir / str(first["context_path"])
    synthetic_private_key_marker = "-----BEGIN " + "PRIVATE KEY-----\nsecret\n"
    context.write_text(synthetic_private_key_marker, encoding="utf-8", newline="\n")
    first["context_sha256"] = _sha256(context)

    with pytest.raises(ValueError, match="^context-sensitive-material:N1$"):
        _validate(manifest, run_dir, roles_dir)


def test_retry_is_limited_to_one_transient_failure(tmp_path: Path) -> None:
    """Prevent a deterministic finding or validation failure from being retried away."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    completed = first["attempts"][0]  # type: ignore[index]
    first["attempts"] = [
        {
            "attempt": 1,
            "status": "failed",
            "error_type": "deterministic",
            "start_event": _event("N1-a1-start", 1),
            "terminal_event": _event("N1-a1-terminal", 2),
            "output_path": None,
            "output_sha256": None,
        },
        {**completed, "attempt": 2, "start_event": _event("N1-a2-start", 3)},
    ]
    first["selected_attempt"] = 2

    with pytest.raises(ValueError, match="^invalid-retry:N1$"):
        _validate(manifest, run_dir, roles_dir)


def test_one_named_transient_failure_may_retry(tmp_path: Path) -> None:
    """Allow one timeout retry while keeping the selected output and events bound."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    completed = first["attempts"][0]  # type: ignore[index]
    first["attempts"] = [
        {
            "attempt": 1,
            "status": "failed",
            "error_type": "timeout",
            "start_event": _event("N1-a1-start", 1),
            "terminal_event": _event("N1-a1-terminal", 2),
            "output_path": None,
            "output_sha256": None,
        },
        {
            **completed,
            "attempt": 2,
            "start_event": _event("N1-a2-start", 3),
            "terminal_event": _event("N1-a2-terminal", 5),
        },
    ]
    first["selected_attempt"] = 2
    first["join_event"] = _event("N1-join", 6)

    assert _validate(manifest, run_dir, roles_dir)["actual_mode"] == "parallel"


def test_cancel_requested_blocks_acceptance_and_cannot_be_joined(tmp_path: Path) -> None:
    """Keep a non-terminal cancelled child from producing a false barrier join."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    manifest = _parallel_manifest(run_dir, roles_dir)
    manifest["claimed_mode"] = "independent-spawned"
    first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    first["attempts"] = [
        {
            "attempt": 1,
            "status": "cancel_requested",
            "error_type": None,
            "start_event": _event("N1-start", 1),
            "terminal_event": None,
            "output_path": None,
            "output_sha256": None,
        }
    ]
    first["join_event"] = None

    summary = _validate(manifest, run_dir, roles_dir)
    assert summary["acceptance_blocked"] is True

    first["join_event"] = _event("N1-join", 6)
    with pytest.raises(ValueError, match="^cancel-requested-joined:N1$"):
        _validate(manifest, run_dir, roles_dir)


def test_read_only_runtime_binds_plan_lineage_terminal_output_and_declared_controls(tmp_path: Path) -> None:
    """Prevent self-attested manifest events from establishing runtime parallelism."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)

    summary = _validate_runtime(
        manifest,
        manifest_path,
        plan_path,
        parent_rollout,
        sessions_dir,
        roles_dir,
    )

    assert summary["actual_mode"] == "parallel"
    assert summary["evidence_level"] == "portable-read-restricted"
    assert summary["network_mode"] == "restricted"
    assert summary["approval_policy"] == "never"
    assert "network_guarantee" not in summary
    assert summary["write_parallel_eligible"] is False
    assert summary["integration_order"] == ["N1", "N2"]
    assert summary["manifest_sha256"] == _sha256(manifest_path)
    assert summary["token_budget_admissions"][0]["wave_id"] == "wave-alpha"
    assert summary["token_budget_admissions"][0]["dispatch_node_ids"] == ["N1", "N2"]
    assert summary["token_budget_admissions"][0]["provider_usage_cap_enforced"] is False
    assert summary["runtime_promotion_eligible"] is False
    assert summary["consumer_id"] is None
    assert "private context" not in repr(summary)
    assert "Evidence from N1" not in repr(summary)
    assert summary["runtime_nodes"][0]["parent_join"] == {
        "event_id": "N1-join",
        "recipient": "/root",
        "message_sha256": hashlib.sha256(
            b"Message Type: FINAL_ANSWER\nTask name: /root\nSender: /root/runtime_n1\nPayload:\nEvidence from N1."
        ).hexdigest(),
    }


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        pytest.param("missing", "runtime-parent-join-missing:N1", id="missing"),
        pytest.param("duplicate", "runtime-parent-join-duplicate:N1", id="duplicate"),
        pytest.param("wrong-sender", "runtime-parent-join-sender-mismatch:N1", id="wrong-sender"),
        pytest.param("wrong-content", "runtime-parent-join-content-mismatch:N1", id="wrong-content"),
        pytest.param("empty-recipient", "runtime-parent-join-recipient-required:N1", id="empty-recipient"),
        pytest.param("early", "runtime-parent-join-before-terminal:N1", id="early"),
        pytest.param("self-attested", "runtime-parent-join-missing:N1", id="self-attested"),
    ),
)
def test_read_only_runtime_host_binds_each_join_to_one_consumed_final_answer(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    """Reject self-attested, malformed, or temporally impossible parent joins."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    rows = [json.loads(line) for line in parent_rollout.read_text(encoding="utf-8").splitlines()]
    join_rows = [
        row
        for row in rows
        if row.get("payload", {}).get("type") == "agent_message" and row.get("payload", {}).get("id") == "N1-join"
    ]
    assert len(join_rows) == 1
    join_row = join_rows[0]
    if mutation == "missing":
        join_row["payload"]["id"] = "different-id"
    elif mutation == "duplicate":
        rows.append(json.loads(json.dumps(join_row)))
    elif mutation == "wrong-sender":
        join_row["payload"]["author"] = "/root/unrelated"
    elif mutation == "wrong-content":
        join_row["payload"]["content"][0]["text"] = "wrong final answer"
    elif mutation == "empty-recipient":
        join_row["payload"]["recipient"] = ""
    elif mutation == "early":
        join_row["timestamp"] = "2026-08-24T12:00:00Z"
    else:
        first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
        first["join_event"] = _event("self-attested-only", 6)  # type: ignore[index]
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    _write_jsonl(parent_rollout, rows)

    with pytest.raises(ValueError, match=f"^{error}$"):
        _validate_runtime(
            manifest,
            manifest_path,
            plan_path,
            parent_rollout,
            sessions_dir,
            roles_dir,
        )


def test_read_only_runtime_rejects_plan_bytes_that_do_not_match_the_approved_digest(tmp_path: Path) -> None:
    """Bind runtime acceptance to the exact frozen plan bytes."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    plan_path.write_text('{"run_id":"changed"}\n', encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="^runtime-plan-hash-mismatch$"):
        _validate_runtime(
            manifest,
            manifest_path,
            plan_path,
            parent_rollout,
            sessions_dir,
            roles_dir,
        )


def test_read_only_runtime_rejects_a_child_without_a_real_terminal_event(tmp_path: Path) -> None:
    """Prevent a parent activity or wait response from masquerading as child completion."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    child_rollout = sessions_dir / "rollout-child-2.jsonl"
    rows = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
    _write_jsonl(
        child_rollout,
        [row for row in rows if row.get("payload", {}).get("type") != "task_complete"],
    )

    with pytest.raises(ValueError, match="^runtime-child-terminal-missing:N2$"):
        _validate_runtime(
            manifest,
            manifest_path,
            plan_path,
            parent_rollout,
            sessions_dir,
            roles_dir,
        )


def test_read_only_runtime_rejects_rollout_schema_drift(tmp_path: Path) -> None:
    """Fail closed when an internal host record changes shape."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    rows = [json.loads(line) for line in parent_rollout.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        item = row.get("payload", {}).get("item", {})
        if item.get("type") == "SubAgentActivity":
            item["type"] = "FutureAgentActivity"
            break
    _write_jsonl(parent_rollout, rows)

    with pytest.raises(ValueError, match="^runtime-parent-start-missing:N1$"):
        _validate_runtime(
            manifest,
            manifest_path,
            plan_path,
            parent_rollout,
            sessions_dir,
            roles_dir,
        )


def test_read_only_runtime_refuses_write_nodes_without_trusted_per_call_controls(tmp_path: Path) -> None:
    """Keep declared thread controls from authorizing write-capable parallel execution."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    first["mutation"] = "write"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="^runtime-write-parallel-unsupported:N1$"):
        _validate_runtime(
            manifest,
            manifest_path,
            plan_path,
            parent_rollout,
            sessions_dir,
            roles_dir,
        )


def test_runtime_declared_model_and_effort_match_the_packaged_role_card(tmp_path: Path) -> None:
    """Prevent a correctly linked child from silently using another runtime policy."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    child_one = sessions_dir / "rollout-child-1.jsonl"
    rows = [json.loads(line) for line in child_one.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        payload = row.get("payload", {})
        if payload.get("type") == "thread_settings_applied":
            payload["thread_settings"]["model"] = "unexpected-model"
        elif row.get("type") == "turn_context":
            payload["model"] = "unexpected-model"
    _write_jsonl(child_one, rows)

    with pytest.raises(ValueError, match="^runtime-role-model-effort-mismatch:N1$"):
        _validate_runtime(
            manifest,
            manifest_path,
            plan_path,
            parent_rollout,
            sessions_dir,
            roles_dir,
        )


def test_runtime_rejects_a_serial_claim_when_child_work_actually_overlaps(tmp_path: Path) -> None:
    """Keep a planned serial label from concealing observed parallel execution."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    manifest["claimed_mode"] = "serial"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="^runtime-mode-claim-mismatch:serial:parallel$"):
        _validate_runtime(
            manifest,
            manifest_path,
            plan_path,
            parent_rollout,
            sessions_dir,
            roles_dir,
        )


def test_runtime_preserves_truthful_serial_fallback_label(tmp_path: Path) -> None:
    """Distinguish an equal-gate fallback from ordinary independent spawning."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    manifest["claimed_mode"] = "serial-fallback"
    child_two = sessions_dir / "rollout-child-2.jsonl"
    rows = [json.loads(line) for line in child_two.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row.get("payload", {}).get("type") == "task_complete":
            row["payload"]["started_at"] = 1_800_000_020
            row["payload"]["completed_at"] = 1_800_000_025
            row["payload"]["duration_ms"] = 5000
    _write_jsonl(child_two, rows)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    summary = _validate_runtime(
        manifest,
        manifest_path,
        plan_path,
        parent_rollout,
        sessions_dir,
        roles_dir,
    )

    assert summary["actual_mode"] == "serial-fallback"


def test_schema_v1_remains_readable_but_cannot_earn_new_runtime_promotion(tmp_path: Path) -> None:
    """Keep historical manifests readable while requiring v2 for new runtime claims."""
    legacy_run_dir = tmp_path / "legacy-run"
    legacy_roles_dir = tmp_path / "legacy-roles"
    legacy_run_dir.mkdir()
    legacy_manifest = _parallel_manifest(legacy_run_dir, legacy_roles_dir)

    assert _validate(legacy_manifest, legacy_run_dir, legacy_roles_dir)["actual_mode"] == "parallel"
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _runtime_fixture(runtime_root)
    with pytest.raises(ValueError, match="^runtime-schema-v2-required$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def test_schema_v2_portable_tier_accepts_restricted_network_without_external_events(tmp_path: Path) -> None:
    """Accept only the bounded portable claim supported by current host evidence."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)

    summary = _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)

    assert summary["actual_mode"] == "parallel"
    assert summary["evidence_level"] == "portable-read-restricted"
    assert summary["network_mode"] == "restricted"
    assert summary["approval_policy"] == "never"
    assert "network_guarantee" not in summary
    assert summary["filesystem_credential_isolation"] == "unverified"


def test_schema_v2_runtime_accepts_subsecond_duration_precision_residual(tmp_path: Path) -> None:
    """Accept millisecond duration consistent with whole-second terminal endpoints."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    child_rollout = sessions_dir / "rollout-child-1.jsonl"
    rows = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        payload = row.get("payload", {})
        if payload.get("type") == "task_complete":
            payload["duration_ms"] += 632
    _write_jsonl(child_rollout, rows)

    summary = _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)

    assert summary["actual_mode"] == "parallel"


def test_schema_v2_runtime_rejects_duration_outside_endpoint_precision(tmp_path: Path) -> None:
    """Reject terminal duration inconsistent with whole-second endpoint precision."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    child_rollout = sessions_dir / "rollout-child-1.jsonl"
    rows = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        payload = row.get("payload", {})
        if payload.get("type") == "task_complete":
            payload["duration_ms"] += 1000
    _write_jsonl(child_rollout, rows)

    with pytest.raises(ValueError, match="^runtime-child-terminal-invalid:N1$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def test_schema_v2_runtime_requires_join_delivery_to_authoritative_parent(tmp_path: Path) -> None:
    """Reject exact child delivery addressed to a path other than the parent."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    rows = [json.loads(line) for line in parent_rollout.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        payload = row.get("payload", {})
        if payload.get("type") == "agent_message" and payload.get("id") == "N1-join":
            payload["recipient"] = "/root/not-parent"
            payload["content"][0]["text"] = payload["content"][0]["text"].replace(
                "Task name: /root", "Task name: /root/not-parent"
            )
    _write_jsonl(parent_rollout, rows)

    with pytest.raises(ValueError, match="^runtime-parent-join-recipient-mismatch:N1$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def test_schema_v2_portable_tier_rejects_legacy_false_node_controls(tmp_path: Path) -> None:
    """Prevent legacy false-denial records from earning a restricted portable promotion."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    first["observed_controls"]["network"] = False  # type: ignore[index]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="^runtime-node-capability-mismatch:N1$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


@pytest.mark.parametrize(
    ("plan", "error"),
    (
        pytest.param(
            {"run_id": "run-1", "capability_policy": {"task_sensitivity": "sensitive"}},
            "runtime-plan-task-sensitivity-mismatch",
            id="run_id-run-1-capability_policy-task_sensitivity-sensitive",
        ),
        pytest.param({"run_id": "run-1"}, "runtime-plan-capability-policy-missing", id="run_id-run-1"),
    ),
)
def test_schema_v2_portable_tier_requires_parent_plan_task_sensitivity(
    tmp_path: Path,
    plan: dict[str, object],
    error: str,
) -> None:
    """Bind portable eligibility to frozen parent-owned sensitivity policy, not manifest self-attestation."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8", newline="\n")
    manifest["plan_sha256"] = _sha256(plan_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match=f"^{error}$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        pytest.param("missing", "runtime-plan-token-budgets-missing", id="missing"),
        pytest.param("overflow", "runtime-token-budget-dispatch-exceeds-admission:wave-alpha", id="overflow"),
        pytest.param("node-mismatch", "runtime-plan-token-budget-node-mismatch:wave-alpha", id="node-mismatch"),
    ),
)
def test_schema_v2_runtime_binds_every_spawned_node_to_the_frozen_token_budget(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    """Reject runtime acceptance when frozen wave reservations did not admit every child."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if mutation == "missing":
        del plan["token_budgets"]
    elif mutation == "overflow":
        plan["token_budgets"][0]["ceiling_tokens"] = 700
    else:
        plan["token_budgets"][0]["node_order"] = ["N2", "N1"]
    plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8", newline="\n")
    manifest["plan_sha256"] = _sha256(plan_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match=f"^{error}$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def test_schema_v2_historical_reader_preserves_unbudgeted_evidence_without_promotion(tmp_path: Path) -> None:
    """Keep earlier unbudgeted schema-v2 evidence readable without reopening admission."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    del plan["token_budgets"]
    plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8", newline="\n")
    manifest["plan_sha256"] = _sha256(plan_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    summary = _validate_runtime(
        manifest,
        manifest_path,
        plan_path,
        parent_rollout,
        sessions_dir,
        roles_dir,
        historical_unbudgeted=True,
    )

    assert summary["acceptance_blocked"] is True
    assert summary["runtime_promotion_eligible"] is False
    assert summary["token_budget_admissions"] == []
    assert summary["evidence_level"] == "historical-portable-read-restricted-unbudgeted"


def test_schema_v2_portable_tier_rejects_restricted_network_with_on_request_approval(
    tmp_path: Path,
) -> None:
    """Prevent approval-gated network access from being presented as portable denial."""
    fixture = _schema_v2_runtime_fixture(tmp_path)
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = fixture
    manifest["capability_evidence"]["network"]["approval_policy"] = "on-request"  # type: ignore[index]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="^portable-network-approval-policy-invalid$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


@pytest.mark.parametrize(
    ("case", "error"),
    (
        pytest.param("malformed", "capability-evidence-invalid", id="malformed"),
        pytest.param("declared-external-event", "portable-external-events-invalid", id="declared-external-event"),
        pytest.param("context-scan-failed", "portable-context-scan-required", id="context-scan-failed"),
        pytest.param(
            "filesystem-isolation-claimed", "portable-filesystem-isolation-invalid", id="filesystem-isolation-claimed"
        ),
    ),
)
def test_schema_v2_portable_tier_rejects_untrusted_capability_evidence(
    tmp_path: Path,
    case: str,
    error: str,
) -> None:
    """Prevent capability self-attestation from expanding portable-read-restricted scope."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    evidence = manifest["capability_evidence"]  # type: ignore[index]
    if case == "malformed":
        evidence["unexpected"] = "field"
    elif case == "declared-external-event":
        evidence["network"]["external_events"] = ["browser.open"]
    elif case == "context-scan-failed":
        evidence["credentials"]["context_scan"] = "failed"
    else:
        evidence["credentials"]["filesystem_isolation"] = "isolated"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match=f"^{error}$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def test_schema_v2_portable_tier_rejects_mixed_host_network_records(tmp_path: Path) -> None:
    """Require every child host record to support the same bounded capability claim."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    child_rollout = sessions_dir / "rollout-child-2.jsonl"
    rows = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        payload = row.get("payload", {})
        if row.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
            payload["thread_settings"]["permission_profile"]["network"] = False
        elif row.get("type") == "turn_context":
            payload["permission_profile"]["network"] = False
    _write_jsonl(child_rollout, rows)

    with pytest.raises(ValueError, match="^portable-network-record-mismatch:N2$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def test_schema_v2_portable_tier_rejects_sensitive_tasks(tmp_path: Path) -> None:
    """Keep sensitive work serial until stronger host isolation is observable."""
    fixture = _schema_v2_runtime_fixture(tmp_path)
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = fixture
    manifest["capability_evidence"]["task_sensitivity"] = "sensitive"  # type: ignore[index]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="^portable-sensitive-task$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def test_schema_v2_portable_tier_rejects_external_network_events(tmp_path: Path) -> None:
    """Reject browser, search, MCP, connector, and network events from local-only waves."""
    fixture = _schema_v2_runtime_fixture(tmp_path)
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = fixture
    child_rollout = sessions_dir / "rollout-child-1.jsonl"
    rows = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
    rows.append(
        {
            "ordinal": 5,
            "timestamp": "2026-08-24T12:00:05Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "browser.open"},
        }
    )
    _write_jsonl(child_rollout, rows)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="^portable-external-network-event$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def test_schema_v2_portable_tier_rejects_secret_material_in_bound_output_and_terminal(tmp_path: Path) -> None:
    """Prevent a hash-valid child result from persisting a common credential pattern."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    secret_message = "ghp_abcdefghijklmnopqrst"
    first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
    attempt = first["attempts"][0]  # type: ignore[index]
    output_path = manifest_path.parent / str(attempt["output_path"])
    output_path.write_text(secret_message + "\n", encoding="utf-8", newline="\n")
    attempt["output_sha256"] = _sha256(output_path)
    child_rollout = sessions_dir / "rollout-child-1.jsonl"
    child_rows = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
    for row in child_rows:
        if row.get("payload", {}).get("type") == "task_complete":
            row["payload"]["last_agent_message"] = secret_message
    _write_jsonl(child_rollout, child_rows)
    parent_rows = [json.loads(line) for line in parent_rollout.read_text(encoding="utf-8").splitlines()]
    for row in parent_rows:
        payload = row.get("payload", {})
        if payload.get("type") == "agent_message" and payload.get("id") == "N1-join":
            payload["content"][0]["text"] = "\n".join(
                (
                    "Message Type: FINAL_ANSWER",
                    "Task name: /root",
                    "Sender: /root/runtime_n1",
                    "Payload:",
                    secret_message,
                )
            )
    _write_jsonl(parent_rollout, parent_rows)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="^runtime-output-sensitive-material:N1$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def test_schema_v2_portable_tier_rejects_unknown_response_item_calls(tmp_path: Path) -> None:
    """Prevent a future host response-item call from silently bypassing the network gate."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    child_rollout = sessions_dir / "rollout-child-1.jsonl"
    rows = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
    rows.append(
        {
            "ordinal": 5,
            "timestamp": "2026-08-24T12:00:05Z",
            "type": "response_item",
            "payload": {"type": "future_tool_call"},
        }
    )
    _write_jsonl(child_rollout, rows)

    with pytest.raises(ValueError, match="^portable-external-network-event$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def test_schema_v2_portable_tier_reports_configuration_not_exec_network_observation(tmp_path: Path) -> None:
    """Prevent local command arguments from being misreported as proof no network attempt occurred."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    child_rollout = sessions_dir / "rollout-child-1.jsonl"
    rows = [json.loads(line) for line in child_rollout.read_text(encoding="utf-8").splitlines()]
    rows.append(
        {
            "ordinal": 5,
            "timestamp": "2026-08-24T12:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec",
                "arguments": json.dumps({"cmd": "curl https://example.invalid && ssh user@example.invalid"}),
            },
        }
    )
    _write_jsonl(child_rollout, rows)

    summary = _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)

    assert summary["evidence_level"] == "portable-read-restricted"
    assert summary["network_mode"] == "restricted"
    assert summary["approval_policy"] == "never"
    assert "network_guarantee" not in summary


def test_schema_v2_host_isolated_tier_requires_authoritative_evidence(tmp_path: Path) -> None:
    """Keep the stronger future tier unavailable when host isolation is unobserved."""
    fixture = _schema_v2_runtime_fixture(tmp_path)
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = fixture
    manifest["capability_evidence"]["tier"] = "host-isolated"  # type: ignore[index]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="^host-isolation-evidence-unavailable$"):
        _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


class TestPortableReadConsumerRuntimeMatrix:
    """Bind each promoted consumer to the portable read-only runtime contract."""

    @pytest.mark.parametrize(
        ("skill", "safe_surface", "serial_surface", "join_clause", "resource_clause"),
        (
            pytest.param(
                "implement",
                "read-only evidence, acceptance, and documentation-impact passes",
                "implementation, test, and documentation writes",
                "join every terminal handoff before implementation, integration, gates, or acceptance",
                "Shared paths, indexes, caches, generated outputs, test environments, ports, devices, or undeclared resources force serial execution or re-planning.",
                id="implement",
            ),
            pytest.param(
                "manage",
                "read-only inventory, reference, ownership, and policy-impact scans",
                "create, update, delete, rename, and permission mutations",
                "join every terminal scan before edits, propagation, gates, or acceptance",
                "Shared targets, paths, indexes, caches, generated outputs, test environments, ports, devices, or undeclared resources force serial execution or re-planning.",
                id="manage",
            ),
        ),
    )
    def test_documents_portable_read_only_contract(
        self,
        skill: str,
        safe_surface: str,
        serial_surface: str,
        join_clause: str,
        resource_clause: str,
    ) -> None:
        """Keep each promoted consumer's documented boundary complete and fail closed."""
        text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
        assert '"execution": "optional auto|serial|parallel-read|parallel-write; default auto"' in text
        heading = "\n## Parallel Adoption (Portable read-only)\n"
        assert heading in text
        section = text.split(heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]

        for subsection in (
            "### Safe parallel work",
            "### Required barrier",
            "### Serial parent decisions",
            "### Resource conflicts",
            "### Fallback",
            "### Acceptance",
            "### Stop rule",
        ):
            assert subsection in section
        for contract in (
            "This skill permits only its promoted portable read-only route.",
            "Resolve execution precedence from per-invocation `--execution=<mode>`, then `CODEX_RIG_EXECUTION`, then the `auto` default.",
            safe_surface,
            serial_surface,
            join_clause,
            resource_clause,
            "The default execution mode is `auto`.",
            "`auto` selects this route only after this consumer's runtime matrix and promotion; otherwise it resolves safely to `serial`.",
            "Every write still requires a frozen plan and exact-digest approval.",
            "Before any dispatch, freeze",
            "Dispatch at most one fixed dependency-ready wave",
            "canonical quality gates; verdict; and promotion",
            "Unavailable or unsafe fan-out uses equal-gate `serial-fallback` from the same frozen plan with the same quality gates and retained evidence.",
            "This skill's shared runtime matrix and consumer promotion must remain complete before `auto` selects this route.",
            "Generic parallel writes remain disabled.",
            "This route never bypasses consumer promotion, serial parent authority, or write approval",
            f"parallel_execution.py preflight --consumer {skill}",
            f"parallel_execution.py validate-runtime --consumer {skill}",
            "Run the same preflight again after the terminal join and before the first parent mutation.",
        ):
            assert contract in section
        for unsafe_exception in (
            "unless `--execution`",
            "unless `CODEX_RIG_EXECUTION`",
            "native evidence is normally required",
            "except approved requests",
            "generic parallel writes may",
            "`parallel-write` enables",
        ):
            assert unsafe_exception not in section

    def test_auto_falls_back_until_read_route_is_promoted(self) -> None:
        """Keep auto serial until promotion, then select only the promoted read route."""
        resolver = _load_validator().resolve_execution_mode
        assert (
            resolver(
                "--execution=auto",
                environment={},
                read_parallel_promoted=False,
                write_parallel_promoted=False,
            )["effective_mode"]
            == "serial"
        )
        assert (
            resolver(
                "--execution=auto",
                environment={},
                read_parallel_promoted=True,
                write_parallel_promoted=False,
            )["effective_mode"]
            == "parallel-read"
        )

    @pytest.mark.parametrize("skill", ("implement", "manage"))
    def test_accepts_promoted_complete_join(self, tmp_path: Path, skill: str) -> None:
        """Accept complete read-only runtime evidence for each promoted consumer."""
        fixture = _schema_v2_runtime_fixture(tmp_path)
        manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = fixture
        _bind_portable_read_consumer_policy(manifest, manifest_path, plan_path, consumer_id=skill)

        summary = _validate_runtime(
            manifest,
            manifest_path,
            plan_path,
            parent_rollout,
            sessions_dir,
            roles_dir,
            expected_consumer_id=skill,
        )

        assert summary["runtime_promotion_eligible"] is True
        assert summary["write_parallel_eligible"] is False
        assert summary["consumer_id"] == skill

    @pytest.mark.parametrize(
        ("skill", "case", "error"),
        (
            pytest.param(
                "implement", "incomplete-join", "runtime-parent-join-missing:N1", id="implement-incomplete-join"
            ),
            pytest.param("implement", "write-authority", "runtime-write-parallel-unsupported:N1", id="implement-write"),
            pytest.param("manage", "incomplete-join", "runtime-parent-join-missing:N1", id="manage-incomplete-join"),
            pytest.param("manage", "write-authority", "runtime-write-parallel-unsupported:N1", id="manage-write"),
        ),
    )
    def test_rejects_incomplete_join_or_write_nodes(
        self,
        tmp_path: Path,
        skill: str,
        case: str,
        error: str,
    ) -> None:
        """Reject missing parent joins and any write-capable child node."""
        fixture = _schema_v2_runtime_fixture(tmp_path)
        manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = fixture
        _bind_portable_read_consumer_policy(manifest, manifest_path, plan_path, consumer_id=skill)
        if case == "incomplete-join":
            rows = [json.loads(line) for line in parent_rollout.read_text(encoding="utf-8").splitlines()]
            for row in rows:
                payload = row.get("payload", {})
                if payload.get("type") == "agent_message" and payload.get("id") == "N1-join":
                    payload["id"] = "missing-join"
            _write_jsonl(parent_rollout, rows)
        else:
            first = manifest["stages"][0]["nodes"][0]  # type: ignore[index]
            first["mutation"] = "write"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError, match=f"^{error}$"):
            _validate_runtime(
                manifest,
                manifest_path,
                plan_path,
                parent_rollout,
                sessions_dir,
                roles_dir,
                expected_consumer_id=skill,
            )


def test_unbound_runtime_evidence_is_valid_but_not_promotion_eligible(tmp_path: Path) -> None:
    """Prevent generic validation from promoting a consumer that was never identified."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)

    summary = _validate_runtime(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)

    assert summary["actual_mode"] == "parallel"
    assert summary["runtime_promotion_eligible"] is False
    assert summary["consumer_id"] is None


@pytest.mark.installed_plugin
@pytest.mark.parametrize("consumer_id", ["implement", "manage"])
def test_consumer_preflight_cli_binds_auto_and_exact_parent_write_approval(tmp_path: Path, consumer_id: str) -> None:
    """Prove the shipped consumer command derives promotion and exact write authority."""
    manifest, manifest_path, plan_path, _parent_rollout, _sessions_dir, _roles_dir = _schema_v2_runtime_fixture(
        tmp_path
    )
    _bind_portable_read_consumer_policy(
        manifest,
        manifest_path,
        plan_path,
        consumer_id=consumer_id,
        parent_writes="planned",
    )
    approval_path = plan_path.with_name("write-approval.json")
    approval_path.write_text(
        json.dumps({"plan_sha256": _sha256(plan_path), "response": "approve", "source": "user-prompt"}) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            "preflight",
            "--consumer",
            consumer_id,
            "--plan",
            str(plan_path),
            "--approval",
            str(approval_path),
            "--execution=auto",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["consumer_id"] == consumer_id
    assert result["requested_mode"] == "auto"
    assert result["effective_mode"] == "parallel-read"
    assert result["write_approval_required"] is True
    assert result["write_approval_validated"] is True
    assert result["plan_sha256"] == _sha256(plan_path)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        pytest.param("plan_sha256", "0" * 64, id="plan_sha256"),
        pytest.param("response", "deny", id="response"),
        pytest.param("source", "environment", id="source"),
    ),
)
def test_consumer_preflight_rejects_each_invalid_write_approval_field(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    """Reject stale, denied, or non-human parent write authority independently."""
    manifest, manifest_path, plan_path, _parent_rollout, _sessions_dir, _roles_dir = _schema_v2_runtime_fixture(
        tmp_path
    )
    _bind_portable_read_consumer_policy(
        manifest, manifest_path, plan_path, consumer_id="implement", parent_writes="planned"
    )
    approval = {"plan_sha256": _sha256(plan_path), "response": "approve", "source": "explicit-input"}
    approval[field] = value
    approval_path = plan_path.with_name("write-approval.json")
    approval_path.write_text(json.dumps(approval) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="^write-approval-invalid$"):
        _load_validator().resolve_consumer_execution_mode(
            "implement",
            "--execution=auto",
            environment={},
            plan_path=plan_path,
            approval_path=approval_path,
        )


def test_consumer_preflight_requires_approval_for_planned_parent_writes(tmp_path: Path) -> None:
    """Prevent a promoted read wave from becoming an approval-free parent write."""
    manifest, manifest_path, plan_path, _parent_rollout, _sessions_dir, _roles_dir = _schema_v2_runtime_fixture(
        tmp_path
    )
    _bind_portable_read_consumer_policy(
        manifest, manifest_path, plan_path, consumer_id="manage", parent_writes="planned"
    )

    with pytest.raises(ValueError, match="^write-approval-required$"):
        _load_validator().resolve_consumer_execution_mode(
            "manage",
            None,
            environment={},
            plan_path=plan_path,
            approval_path=None,
        )


@pytest.mark.parametrize("consumer_id", ["implement", "manage"])
def test_consumer_runtime_cli_requires_identity_and_returns_bound_summary(tmp_path: Path, consumer_id: str) -> None:
    """Exercise the shipped post-join consumer call instead of a test-only helper."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    _bind_portable_read_consumer_policy(manifest, manifest_path, plan_path, consumer_id=consumer_id)

    completed = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            "validate-runtime",
            "--consumer",
            consumer_id,
            "--manifest",
            str(manifest_path),
            "--plan",
            str(plan_path),
            "--parent-rollout",
            str(parent_rollout),
            "--sessions-dir",
            str(sessions_dir),
            "--run-dir",
            str(manifest_path.parent),
            "--roles-dir",
            str(roles_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["consumer_id"] == consumer_id
    assert summary["runtime_promotion_eligible"] is True
    assert summary["write_parallel_eligible"] is False


@pytest.mark.parametrize("consumer_id", ["implement", "manage"])
@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        pytest.param("consumer_id", "other", "runtime-consumer-id-mismatch", id="consumer_id"),
        pytest.param("capability", "parallel-write", "runtime-consumer-capability-invalid", id="capability"),
        pytest.param("promotion_status", "pending", "runtime-consumer-promotion-required", id="promotion_status"),
        pytest.param(
            "parent_mutations", "parallel", "runtime-consumer-parent-mutations-invalid", id="parent_mutations"
        ),
        pytest.param("canonical_gates", "parallel", "runtime-consumer-canonical-gates-invalid", id="canonical_gates"),
    ),
)
def test_portable_read_runtime_requires_exact_promoted_consumer_policy(
    tmp_path: Path,
    consumer_id: str,
    field: str,
    value: str,
    error: str,
) -> None:
    """Reject a consumer plan that loosens portable-read-only parent boundaries."""
    manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir = _schema_v2_runtime_fixture(tmp_path)
    _bind_portable_read_consumer_policy(manifest, manifest_path, plan_path, consumer_id=consumer_id)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["consumer_policy"][field] = value
    plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8", newline="\n")
    manifest["plan_sha256"] = _sha256(plan_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match=f"^{error}$"):
        _validate_runtime(
            manifest,
            manifest_path,
            plan_path,
            parent_rollout,
            sessions_dir,
            roles_dir,
            expected_consumer_id=consumer_id,
        )


def test_token_budget_admits_a_stable_prefix_and_preserves_existing_work() -> None:
    """Prevent budget exhaustion from replaying completed work or killing active children."""
    result = _load_validator().admit_wave_token_budget(
        ceiling_tokens=100,
        node_order=["done", "active", "next", "later"],
        reservations={"done": 20, "active": 30, "next": 40, "later": 20},
        completed_node_ids=["done"],
        active_node_ids=["active"],
    )

    assert result == {
        "schema_version": 1,
        "enforcement_scope": "pre-dispatch-reservations",
        "ceiling_tokens": 100,
        "reserved_tokens": 90,
        "remaining_tokens": 10,
        "dispatch_node_ids": ["next"],
        "completed_node_ids": ["done"],
        "active_node_ids": ["active"],
        "serial_replan_node_ids": ["later"],
        "exhausted": True,
        "active_child_policy": "await-terminal-evidence",
        "completed_work_policy": "preserve",
        "unfinished_work_policy": "serial-replan-same-gates",
        "provider_usage_cap_enforced": False,
    }


@pytest.mark.parametrize(
    ("overrides", "error"),
    (
        pytest.param({"ceiling_tokens": 0}, "token-budget-ceiling-invalid", id="ceiling_tokens-0"),
        pytest.param({"node_order": "first"}, "token-budget-node-order-invalid", id="node_order-first"),
        pytest.param(
            {"reservations": ["first", "second"]}, "token-reservations-invalid", id="reservations-first-second"
        ),
        pytest.param(
            {"reservations": {"first": True, "second": 20}},
            "token-reservation-invalid:first",
            id="reservations-first-true-second-20",
        ),
        pytest.param({"reservations": {"first": 20}}, "token-reservation-node-mismatch", id="reservations-first-20"),
        pytest.param(
            {"completed_node_ids": "first"}, "token-budget-completed-state-invalid", id="completed_node_ids-first"
        ),
        pytest.param(
            {"active_node_ids": ["second"]}, "token-budget-existing-state-not-prefix", id="active_node_ids-second"
        ),
        pytest.param(
            {"completed_node_ids": ["first"], "active_node_ids": ["first"]},
            "token-budget-state-overlap:first",
            id="completed_node_ids-first-active_node_ids-first",
        ),
        pytest.param(
            {"ceiling_tokens": 10, "completed_node_ids": ["first"]},
            "token-budget-already-exceeded",
            id="ceiling_tokens-10-completed_node_ids-first",
        ),
    ),
)
def test_token_budget_rejects_unsafe_or_incoherent_admission_state(
    overrides: dict[str, object],
    error: str,
) -> None:
    """Fail closed when a frozen reservation budget cannot prove bounded admission."""
    arguments: dict[str, object] = {
        "ceiling_tokens": 100,
        "node_order": ["first", "second"],
        "reservations": {"first": 20, "second": 20},
        "completed_node_ids": [],
        "active_node_ids": [],
    }
    arguments.update(overrides)

    with pytest.raises(ValueError, match=f"^{error}$"):
        _load_validator().admit_wave_token_budget(**arguments)


def test_parallel_rollback_contract_preserves_identity_evidence_and_gates() -> None:
    """Prevent operator rollback wording from authorizing replay or weaker serial checks."""
    architecture = (PLUGIN_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    for text in (architecture, readme):
        for contract in (
            "Disable the affected skill's parallel opt-in without changing the frozen plan or its digest.",
            "Preserve completed outputs, terminal child evidence, parent joins, and the original quality gates.",
            "Serially execute only unfinished work; never replay completed nodes.",
            "Retain failed or conflicted worktrees and stop when cleanup or repository state is ambiguous.",
        ):
            assert contract in text
