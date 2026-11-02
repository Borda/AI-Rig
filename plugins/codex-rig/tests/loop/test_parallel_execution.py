"""Acceptance checks for truthful staged multi-agent execution evidence.

Drives the standalone ``shared/parallel_execution.py`` validator through its public functions and shipped CLI: execution
mode resolution, schema-v1 manifests, schema-v2 runtime evidence built from rollout-shaped host records, token-budget
admission, consumer preflight, and review-input admission. Manifests and rollouts are synthesised in ``tmp_path``.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import itertools
import json
import subprocess
import sys
import tempfile  # noqa: F401
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest
from _artifact_files import read_jsonl
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PLUGIN_ROOT / "skills"
VALIDATOR_PATH = PLUGIN_ROOT / "shared" / "parallel_execution.py"

#: Manifest path to the first node of the first stage, shared by field-mutation cases.
_NODE = ("stages", 0, "nodes", 0)
#: Manifest path to the schema-v2 capability evidence record.
_EVIDENCE = ("capability_evidence",)


def _load_validator() -> ModuleType:
    """Load the standalone installed-package helper by file path."""
    return load_module("codex_rig_parallel_execution", VALIDATOR_PATH)


def _sha256(path: Path) -> str:
    """Return the exact SHA-256 digest for one fixture file.

    Example:
        >>> len(_sha256(PLUGIN_ROOT / "package-manifest.json"))
        64
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _squash(text: str) -> str:
    """Collapse every whitespace run so prose assertions ignore where a paragraph wraps."""
    return " ".join(text.split())


def _section_body(text: str, heading: str) -> str:
    """Return the lines under a level-two heading up to the next level-two heading."""
    lines = text.splitlines()
    body = itertools.takewhile(lambda line: not line.startswith("## "), lines[lines.index(heading) + 1 :])
    return "\n".join(body)


def _set_path(root: Any, path: tuple[str | int, ...], value: object) -> Any:
    """Replace the value at a nested key and index path and return the root."""
    node = root
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return root


def _assigning(*edits: tuple[tuple[str | int, ...], object]) -> Callable[[Any], Any]:
    """Return a mutator that assigns each ``(path, value)`` edit into a root and returns it."""

    def mutate(root: Any) -> Any:
        for path, value in edits:
            _set_path(root, path, copy.deepcopy(value))
        return root

    return mutate


def _write_json(path: Path, document: object, *, indent: int | None = None) -> None:
    """Write one JSON document with a trailing newline and portable line endings."""
    path.write_text(json.dumps(document, indent=indent) + "\n", encoding="utf-8", newline="\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write one rollout-shaped fixture with portable newlines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


@contextlib.contextmanager
def _editing(path: Path) -> Iterator[list[dict[str, Any]]]:
    """Yield a rollout's rows for in-place edits and write them back when the block exits cleanly."""
    rows = read_jsonl(path)
    yield rows
    _write_jsonl(path, rows)


def _select(
    rows: list[dict[str, Any]],
    *,
    row_type: str | None = None,
    payload_type: str | None = None,
    payload_id: str | None = None,
    item_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the rows whose type, payload type, payload id, and item id match every filter that was given."""

    def matches(row: dict[str, Any]) -> bool:
        payload = row.get("payload", {})
        return (
            (row_type is None or row.get("type") == row_type)
            and (payload_type is None or payload.get("type") == payload_type)
            and (payload_id is None or payload.get("id") == payload_id)
            and (item_id is None or payload.get("item", {}).get("id") == item_id)
        )

    return [row for row in rows if matches(row)]


def _only(rows: list[dict[str, Any]], **selector: str) -> dict[str, Any]:
    """Return the single row chosen by ``selector``, failing when none or several match."""
    selected = _select(rows, **selector)
    assert len(selected) == 1, selector
    return selected[0]


def _assign(rows: list[dict[str, Any]], path: tuple[str | int, ...], value: object, **selector: str) -> None:
    """Set the nested ``path`` to ``value`` on every row chosen by ``selector``."""
    for row in _select(rows, **selector):
        _set_path(row, path, copy.deepcopy(value))


def _nested_parent_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Model nested native history whose metadata order differs from its identity chain."""
    owner = {
        **rows[0]["payload"],
        "multi_agent_version": "v2",
        "thread_source": "subagent",
        "history_mode": "paginated",
        "subagent_history_start_ordinal": 4,
        "parent_thread_id": "middle",
        "forked_from_id": "middle",
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "middle"}}},
    }
    middle = {
        "id": "middle",
        "parent_thread_id": "ancestor",
        "forked_from_id": "ancestor",
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "ancestor"}}},
    }
    inherited = {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "inherited"}}
    result = [
        {**rows[0], "payload": owner},
        {"type": "session_meta", "payload": {"id": "ancestor"}},
        {"type": "session_meta", "payload": middle},
        inherited,
        *rows[1:],
    ]
    return [{**row, "ordinal": index} for index, row in enumerate(result)]


def _write_role(roles_dir: Path, role_id: str) -> str:
    """Write one canonical-card-shaped fixture and return its digest.

    Example:
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     len(_write_role(Path(directory), "qa-specialist"))
        64
    """
    path = roles_dir / role_id / "ROLE.md"
    effort = "medium" if role_id == "qa-specialist" else "high"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            (
                "---",
                f"role_id: {role_id}",
                f"name: codex-rig-{role_id}",
                "model: gpt-6.1-sol",
                f"model_reasoning_effort: {effort}",
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
    controls = {"sandbox_mode": "read-only", "write_paths": [], "network": False, "credentials": False}
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


def _parallel_manifest(run_dir: Path, roles_dir: Path) -> dict[str, Any]:
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
                        run_dir, roles_dir, node_id="N1", role_id="qa-specialist", start=1, terminal=4, join=6
                    ),
                    _completed_node(
                        run_dir, roles_dir, node_id="N2", role_id="challenger", start=2, terminal=5, join=7
                    ),
                ],
            }
        ],
    }


def _nodes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the nodes of the first stage."""
    return manifest["stages"][0]["nodes"]


class ParallelWave(NamedTuple):
    """A valid two-node schema-v1 wave together with the directories holding its evidence files."""

    manifest: dict[str, Any]
    run_dir: Path
    roles_dir: Path

    def validate(self) -> dict[str, Any]:
        """Validate through the public installed-package function."""
        return _load_validator().validate_execution_manifest(
            self.manifest, run_dir=self.run_dir, roles_dir=self.roles_dir
        )


@pytest.fixture
def wave(tmp_path: Path) -> ParallelWave:
    """Build a valid two-node parallel wave with hash-bound context, output, and role-card files on disk."""
    run_dir = tmp_path / "run"
    roles_dir = tmp_path / "roles"
    run_dir.mkdir()
    return ParallelWave(_parallel_manifest(run_dir, roles_dir), run_dir, roles_dir)


def _make_write_node(node: dict[str, Any], path: str, locks: tuple[str, ...] = ()) -> None:
    """Turn a read-only node into a workspace-write node owning one path."""
    controls = {"sandbox_mode": "workspace-write", "write_paths": [path], "network": False, "credentials": False}
    node.update(
        mutation="write",
        owned_paths=[path],
        resource_locks=list(locks),
        requested_controls=controls,
        observed_controls={**controls, "enforced": True},
    )


def _make_write_nodes(manifest: dict[str, Any], paths: tuple[str, ...], locks: tuple[str, ...] = ()) -> None:
    """Turn the first stage's nodes into write nodes, one owned path each."""
    for node, path in zip(_nodes(manifest), paths, strict=True):
        _make_write_node(node, path, locks)


def _valid_write_approval(manifest: dict[str, Any]) -> dict[str, str]:
    """Return an approval bound to the manifest's frozen plan digest."""
    return {"plan_sha256": manifest["plan_sha256"], "response": "approve", "source": "explicit-input"}


def _serial_write_wave(wave: ParallelWave) -> dict[str, Any]:
    """Reduce a wave to one serial write node owning ``src/feature.py`` and return its manifest."""
    wave.manifest["claimed_mode"] = "serial"
    wave.manifest["stages"][0]["nodes"] = _nodes(wave.manifest)[:1]
    _make_write_node(_nodes(wave.manifest)[0], "src/feature.py")
    return wave.manifest


def _failed_attempt(error_type: str) -> dict[str, Any]:
    """Return a first attempt that failed with the given error type and produced no output."""
    return {
        "attempt": 1,
        "status": "failed",
        "error_type": error_type,
        "start_event": _event("N1-a1-start", 1),
        "terminal_event": _event("N1-a1-terminal", 2),
        "output_path": None,
        "output_sha256": None,
    }


def _cancel_requested_node(wave: ParallelWave, join_event: dict[str, object] | None) -> None:
    """Replace the first node's attempts with a non-terminal cancel request and the given join."""
    wave.manifest["claimed_mode"] = "independent-spawned"
    first = _nodes(wave.manifest)[0]
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
    first["join_event"] = join_event


class RuntimeRun(NamedTuple):
    """Paths and manifest of one on-disk runtime fixture with two host-bound read-only children."""

    manifest: dict[str, Any]
    manifest_path: Path
    plan_path: Path
    parent_rollout: Path
    sessions_dir: Path
    roles_dir: Path

    def child_rollout(self, index: int) -> Path:
        """Return the rollout of the child with the given one-based index."""
        return self.sessions_dir / f"rollout-child-{index}.jsonl"

    def read_plan(self) -> dict[str, Any]:
        """Return the frozen plan as currently written."""
        return json.loads(self.plan_path.read_text(encoding="utf-8"))

    def save_manifest(self) -> None:
        """Persist the in-memory manifest."""
        _write_json(self.manifest_path, self.manifest, indent=2)

    def save_plan(self, plan: dict[str, Any]) -> None:
        """Replace the frozen plan and rebind the manifest digest to its new bytes."""
        _write_json(self.plan_path, plan)
        self.manifest["plan_sha256"] = _sha256(self.plan_path)
        self.save_manifest()

    def validate(
        self, *, historical_unbudgeted: bool = False, expected_consumer_id: str | None = None
    ) -> dict[str, Any]:
        """Validate one read-only run against rollout-shaped host evidence."""
        return _load_validator().validate_read_only_runtime(
            self.manifest,
            manifest_path=self.manifest_path,
            plan_path=self.plan_path,
            parent_rollout=self.parent_rollout,
            sessions_dir=self.sessions_dir,
            run_dir=self.manifest_path.parent,
            roles_dir=self.roles_dir,
            historical_unbudgeted=historical_unbudgeted,
            expected_consumer_id=expected_consumer_id,
        )


def _runtime_fixture(tmp_path: Path) -> RuntimeRun:
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
    _write_json(plan_path, plan)
    manifest = _parallel_manifest(run_dir, roles_dir)
    manifest["plan_sha256"] = _sha256(plan_path)
    parent_thread_id = "parent-thread"
    parent_turn_id = "parent-turn"
    parent_rows: list[dict[str, Any]] = [
        {
            "ordinal": 1,
            "timestamp": "2026-08-24T12:00:00Z",
            "type": "session_meta",
            "payload": {"id": parent_thread_id},
        }
    ]
    for index, node in enumerate(_nodes(manifest), start=1):
        effort = "medium" if node["role_id"] == "qa-specialist" else "high"
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
                            "model": "gpt-6.1-sol",
                            "reasoning_effort": effort,
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
                        "model": "gpt-6.1-sol",
                        "effort": effort,
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
    _write_json(manifest_path, manifest, indent=2)
    return RuntimeRun(manifest, manifest_path, plan_path, parent_rollout, sessions_dir, roles_dir)


def _schema_v2_runtime_fixture(tmp_path: Path) -> RuntimeRun:
    """Create a portable schema-v2 run with restricted, approval-free networking."""
    run = _runtime_fixture(tmp_path)
    run.manifest["schema_version"] = 2
    run.manifest["capability_evidence"] = {
        "tier": "portable",
        "task_sensitivity": "non-sensitive",
        "network": {"mode": "restricted", "approval_policy": "never", "external_events": []},
        "credentials": {"context_scan": "passed", "filesystem_isolation": "unverified"},
    }
    for node in _nodes(run.manifest):
        controls = {
            "sandbox_mode": "read-only",
            "write_paths": [],
            "network": "restricted",
            "credentials": "unverified",
        }
        node["requested_controls"] = controls
        node["observed_controls"] = {**controls, "enforced": True}
        child_rollout = run.sessions_dir / f"rollout-{node['attempts'][0]['agent_thread_id']}.jsonl"
        with _editing(child_rollout) as rows:
            settings = {"row_type": "event_msg", "payload_type": "thread_settings_applied"}
            _assign(rows, ("payload", "thread_settings", "approval_policy"), "never", **settings)
            _assign(rows, ("payload", "thread_settings", "permission_profile", "network"), "restricted", **settings)
            _assign(rows, ("payload", "approval_policy"), "never", row_type="turn_context")
            _assign(rows, ("payload", "permission_profile", "network"), "restricted", row_type="turn_context")
    run.save_manifest()
    return run


@pytest.fixture
def v2_run(tmp_path: Path) -> RuntimeRun:
    """Build a portable schema-v2 run: two read-only children with overlapping intervals and restricted networking."""
    return _schema_v2_runtime_fixture(tmp_path)


def _bind_portable_read_consumer_policy(
    run: RuntimeRun, *, consumer_id: str, parent_writes: str = "none", host_approval_policy: str = "never"
) -> None:
    """Bind a fixture's frozen plan to one promoted portable read-only consumer."""
    plan = run.read_plan()
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
    plan["read_host"] = {
        "source": "runtime-tool-contract",
        "sandbox_mode": "read-only",
        "approval_policy": host_approval_policy,
    }
    run.save_plan(plan)


def _write_approval(plan_path: Path, **fields: str) -> Path:
    """Write a parent write approval bound to the plan's digest, with ``fields`` overriding the defaults."""
    approval = {"plan_sha256": _sha256(plan_path), "response": "approve", "source": "explicit-input"} | fields
    path = plan_path.with_name("write-approval.json")
    _write_json(path, approval)
    return path


def _resolve_consumer(
    plan_path: Path, consumer_id: str, execution: str | None, approval_path: Path | None
) -> dict[str, Any]:
    """Resolve a consumer's execution mode through the public preflight function."""
    return _load_validator().resolve_consumer_execution_mode(
        consumer_id, execution, environment={}, plan_path=plan_path, approval_path=approval_path
    )


def _run_validator_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped validator command-line interface in a real child interpreter."""
    return subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), *arguments], capture_output=True, text=True, check=False
    )


def _append_child_row(run: RuntimeRun, payload: dict[str, Any]) -> None:
    """Append a response-item row with ``payload`` to the first child's rollout."""
    with _editing(run.child_rollout(1)) as rows:
        rows.append({"ordinal": 5, "timestamp": "2026-08-24T12:00:05Z", "type": "response_item", "payload": payload})


def _fixture_admission(text: str, start: int, end: int) -> dict[str, object]:
    """Bind approval to one occurrence in the complete synthetic input."""
    return {
        "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "match_start": start,
        "match_end": end,
        "match_sha256": hashlib.sha256(text[start:end].encode("utf-8")).hexdigest(),
        "approval_evidence": "User approved the synthetic fixture.",
        "rationale": "The value is inert test data.",
    }


_CONSUMER_IDS = ["code-review", "implement", "manage"]
#: Where a child's filesystem profile lives: the selector picks the host record, the path leads to its profile.
_CONTROL_RECORDS = [
    pytest.param(
        {"payload_type": "thread_settings_applied"},
        ("payload", "thread_settings", "permission_profile"),
        id="thread-settings",
    ),
    pytest.param({"row_type": "turn_context"}, ("payload", "permission_profile"), id="turn-context"),
]
_SERIAL_ROUTE_FIELDS = ("execution", "host_approval", "requested", "fallback")
_SERIAL_ROUTES = [
    pytest.param("--execution=serial", "never", "serial", None, id="serial"),
    pytest.param(
        "--execution=auto", "on-request", "auto", "host-controls-unavailable-before-dispatch", id="serial-fallback"
    ),
]
_PORTABLE_SUBSECTIONS = (
    "### Safe parallel work",
    "### Required barrier",
    "### Serial parent decisions",
    "### Resource conflicts",
    "### Fallback",
    "### Acceptance",
    "### Stop rule",
)
_PORTABLE_CONTRACTS = (
    "This portable section permits only its promoted portable read-only route",
    "Execution Route Selection must resolve to `parallel-read` before this section applies",
    "`auto` does not select it.",
    "Serial parent work uses existing task authorization; exact-plan-digest approval applies when the promoted parallel-read route is selected.",
    "Before any dispatch, freeze",
    "Dispatch at most one fixed dependency-ready wave",
    "canonical quality gates; verdict; and promotion",
    "Unavailable or unsafe fan-out uses equal-gate `serial-fallback` from the same frozen plan with the same quality gates and retained evidence.",
    "This skill's shared runtime matrix and consumer promotion must remain complete before the selected `parallel-read` route starts.",
    "This route never bypasses consumer promotion, serial parent authority, or applicable write approval",
    "Run the same preflight again after the terminal join and before the first parent mutation.",
)
_PORTABLE_UNSAFE_EXCEPTIONS = (
    "unless `--execution`",
    "unless `CODEX_RIG_EXECUTION`",
    "native evidence is normally required",
    "except approved requests",
    "generic parallel writes may",
    "`parallel-write` enables",
)
_ROLLBACK_CLAUSES = (
    "Disable the affected skill's parallel opt-in without changing the frozen plan or its digest.",
    "Preserve completed outputs, terminal child evidence, parent joins, and the original quality gates.",
    "Serially execute only unfinished work; never replay completed nodes.",
    "Retain failed or conflicted worktrees and stop when cleanup or repository state is ambiguous.",
)

_BEARER_MATCH = "Authorization: Bearer fixture-value"
_BEARER_TEXT = "fixture: " + _BEARER_MATCH
_BEARER_ADMISSION = _fixture_admission(_BEARER_TEXT, 9, len(_BEARER_TEXT))
_REPEATED_TEXT = _BEARER_MATCH + " " + _BEARER_MATCH
_REPEATED_FIRST = _fixture_admission(_REPEATED_TEXT, 0, len(_BEARER_MATCH))
_REPEATED_SECOND = _fixture_admission(_REPEATED_TEXT, len(_BEARER_MATCH) + 1, len(_REPEATED_TEXT))
_OVERLAP_TOKEN_VALUE = "sk-" + "a" * 24
_OVERLAP_TEXT = "Authorization: Bearer " + _OVERLAP_TOKEN_VALUE
_OVERLAP_BEARER = _fixture_admission(_OVERLAP_TEXT, 0, len(_OVERLAP_TEXT))
_OVERLAP_TOKEN = _fixture_admission(_OVERLAP_TEXT, len(_OVERLAP_TEXT) - len(_OVERLAP_TOKEN_VALUE), len(_OVERLAP_TEXT))
_KEY_MARKER = "-----BEGIN " + "PRIVATE KEY-----"
_KEY_TEXT = _KEY_MARKER + "\nsynthetic fixture body\n"
_KEY_ADMISSION = _fixture_admission(_KEY_TEXT, 0, len(_KEY_MARKER))
_JSON_PREFIX = 'é\r\n{"header":"'
_JSON_TEXT = _JSON_PREFIX + 'Authorization: Bearer fixture-value"}'
_JSON_ADMISSION = _fixture_admission(_JSON_TEXT, len(_JSON_PREFIX), len(_JSON_TEXT))

_PAGED_TOKEN = "sk-" + "e" * 24
_PAGED_TEXT = "é\r\nAuthorization: Bearer " + _PAGED_TOKEN
_PAGED_UNKNOWN = "Authorization: Bearer unverified-value"
_PAGED_INPUTS = {"paged:challenger": _PAGED_TEXT, "paged:qa-specialist": _PAGED_UNKNOWN}
_DIRECT_TEXT = "Authorization: Bearer inert-value"


def _grant_child_access(
    run: RuntimeRun, tmp_path: Path, selector: dict[str, str], profile_path: tuple[str, ...], access: str
) -> None:
    """Give the first child a restricted filesystem profile with one entry of the given access."""
    profile = {"type": "restricted", "entries": [{"access": access, "path": str(tmp_path)}]}
    with _editing(run.child_rollout(1)) as rows:
        _assign(rows, (*profile_path, "file_system"), profile, **selector)


@pytest.fixture
def inspection(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    """Build a frozen inspection plan whose CRLF context file admits one private-key marker."""
    text = "é\r\n" + _KEY_MARKER + "\r\nsynthetic body\r\n"
    context = tmp_path / "context.txt"
    context.write_bytes(text.encode("utf-8"))
    plan = {
        "contexts": [{"role_id": "reviewer", "context_path": "context.txt", "context_sha256": _sha256(context)}],
        "inert_fixture_admissions": [_fixture_admission(text, 3, 3 + len(_KEY_MARKER))],
    }
    return plan, context


@pytest.fixture
def verified_selections() -> dict[str, Any]:
    """Build the parent-verified selection of both matches in the challenger input, bound to their real digests."""
    discovered = _load_validator().review_fixture_discovery(_PAGED_INPUTS)["inputs"][0]["matches"]
    return {
        "schema_version": 1,
        "inputs": [
            {
                "input_id": "paged:challenger",
                "complete_input_verified": True,
                "verification_evidence": "Parent verified both complete current fixture inputs against their source.",
                "matches": [
                    {**match, "match_sha256": hashlib.sha256(value.encode()).hexdigest()}
                    for match, value in zip(
                        discovered, ("Authorization: Bearer " + _PAGED_TOKEN, _PAGED_TOKEN), strict=True
                    )
                ],
            }
        ],
    }


@pytest.fixture
def direct_selection() -> dict[str, Any]:
    """Build a valid parent-verified selection of the single bearer header in the direct plan input."""
    return {
        "schema_version": 1,
        "inputs": [
            {
                "input_id": "direct-plan",
                "complete_input_verified": True,
                "verification_evidence": "Parent verified the complete current input, not only its matched values.",
                "matches": [
                    {
                        "pattern_index": 1,
                        "match_start": 0,
                        "match_end": len(_DIRECT_TEXT),
                        "match_sha256": hashlib.sha256(_DIRECT_TEXT.encode()).hexdigest(),
                    }
                ],
            }
        ],
    }


class TestParentHistory:
    """Nested native history projects to the owner's own rows only when its declared boundary is provable."""

    def test_preserves_unbounded_historical_rows(self) -> None:
        """Historical validation retains ambiguous metadata rather than silently normalizing it.

        Two session-meta rows carry no nested-history declaration, so the projection must hand back the very same list.
        """
        rows = [
            {"type": "session_meta", "payload": {"id": "owner"}},
            {"type": "session_meta", "payload": {"id": "ancestor"}},
        ]
        assert _load_validator()._runtime_parent_rows(rows) is rows

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(_assigning(), id="forked-subagent"),
            pytest.param(
                _assigning(
                    ((1, "payload", "parent_thread_id"), "execution-parent-not-inherited"),
                    (
                        (1, "payload", "source"),
                        {"subagent": {"thread_spawn": {"parent_thread_id": "execution-parent-not-inherited"}}},
                    ),
                ),
                id="forkless-subagent",
            ),
        ],
    )
    def test_excludes_inherited_evidence(self, mutate: Callable[[Any], Any]) -> None:
        """Nested metadata is admissible while inherited terminal evidence stays unavailable.

        The owner row and its own task start survive; ancestor metadata and the inherited completion are dropped,
        whether the ancestor chain is a fork or a forkless subagent spawn.
        """
        rows = mutate(
            _nested_parent_history(
                [
                    {"type": "session_meta", "payload": {"id": "owner"}},
                    {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "own"}},
                ]
            )
        )

        projected = _load_validator()._runtime_parent_rows(rows)

        assert projected == [rows[0], rows[4]]
        assert projected[0]["payload"]["id"] == "owner"

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(_assigning(((0, "payload", "subagent_history_start_ordinal"), True)), id="bool-boundary"),
            pytest.param(_assigning(((0, "payload", "subagent_history_start_ordinal"), 0)), id="zero-boundary"),
            # Five rows are modelled, so six lies one past the end.
            pytest.param(_assigning(((0, "payload", "subagent_history_start_ordinal"), 6)), id="large-boundary"),
            pytest.param(_assigning(((0, "payload", "subagent_history_start_ordinal"), "4")), id="string-boundary"),
            pytest.param(_assigning(((0, "payload", "multi_agent_version"), "unsupported")), id="unsupported-version"),
            pytest.param(_assigning(((0, "payload", "thread_source"), "unsupported")), id="unsupported-source"),
            pytest.param(_assigning(((0, "payload", "history_mode"), "unsupported")), id="unsupported-history"),
            pytest.param(_assigning(((0, "ordinal"), False)), id="bool-ordinal"),
            pytest.param(_assigning(((2, "ordinal"), 10)), id="gap-ordinal"),
            pytest.param(
                _assigning(((4,), {"type": "session_meta", "ordinal": 4, "payload": {"id": "late"}})),
                id="late-metadata",
            ),
            pytest.param(_assigning(((1, "payload", "id"), "owner")), id="duplicate-owner"),
            pytest.param(_assigning(((1, "payload", "id"), "")), id="empty-id"),
            pytest.param(_assigning(((0, "payload", "forked_from_id"), "ancestor")), id="contradictory-parent"),
            pytest.param(
                _assigning(
                    ((1, "payload", "parent_thread_id"), "execution-parent"),
                    (
                        (1, "payload", "source"),
                        {"subagent": {"thread_spawn": {"parent_thread_id": "different-parent"}}},
                    ),
                ),
                id="forkless-parent-conflict",
            ),
            pytest.param(
                _assigning(((1, "payload", "subagent_history_start_ordinal"), 1)), id="forkless-declared-history"
            ),
            pytest.param(_assigning(((2, "payload"), {"id": "middle"})), id="disconnected"),
            pytest.param(
                _assigning(
                    ((2, "payload", "parent_thread_id"), "owner"),
                    ((2, "payload", "forked_from_id"), "owner"),
                    ((2, "payload", "source"), {"subagent": {"thread_spawn": {"parent_thread_id": "owner"}}}),
                ),
                id="cycle",
            ),
        ],
    )
    def test_rejects_unproven_boundaries(self, mutate: Callable[[Any], Any]) -> None:
        """A malformed native declaration never turns inherited events into owner provenance.

        Starting from a valid nested history, each case damages the boundary ordinal, version fields, row ordinals,
        metadata placement, or identity chain; every one must fail closed.
        """
        rows = mutate(
            _nested_parent_history(
                [
                    {"type": "session_meta", "payload": {"id": "owner"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                ]
            )
        )

        with pytest.raises(ValueError, match="runtime-parent-history-invalid"):
            _load_validator()._runtime_parent_rows(rows)


def _spawn_history(extra_arguments: dict[str, str], spawn_turn_id: str) -> list[dict[str, Any]]:
    """Return a turn context for ``turn-1`` followed by a spawn call attributed to ``spawn_turn_id``."""
    arguments = {"agent_type": "qa-specialist", "task_name": "reviewer", **extra_arguments}
    return [
        {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "model-a", "effort": "medium"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "spawn-1",
                "arguments": json.dumps(arguments),
                "internal_chat_message_metadata_passthrough": {"turn_id": spawn_turn_id},
            },
        },
    ]


def _spawn_kwargs(model: str | None, effort: str | None) -> dict[str, Any]:
    """Return the keyword arguments identifying the spawn and the dispatch choice made for it."""
    return {
        "call_id": "spawn-1",
        "role_id": "qa-specialist",
        "agent_path": "/root/reviewer",
        "node_id": "node-1",
        "choice": {"role_id": "qa-specialist", "model": model, "reasoning_effort": effort},
    }


_INHERITANCE_CASES = pytest.mark.parametrize(
    ("extra_arguments", "model", "effort"),
    [
        pytest.param({}, None, None, id="both-inherited"),
        pytest.param({"model": "model-a"}, "model-a", None, id="effort-inherited"),
        pytest.param({"reasoning_effort": "medium"}, None, "medium", id="model-inherited"),
    ],
)


class TestSpawnControls:
    """Model and effort a spawn inherits from its parent are only valid when taken from the spawn's own turn."""

    @_INHERITANCE_CASES
    def test_accepts_controls_from_spawn_turn(
        self, extra_arguments: dict[str, str], model: str | None, effort: str | None
    ) -> None:
        """Inherited controls resolve from the turn that issued the spawn.

        The spawn names turn-1, which carries model-a at medium effort; whichever of model or effort the spawn leaves
        implicit must resolve to that turn's values.
        """
        rows = _spawn_history(extra_arguments, "turn-1")

        call = _load_validator()._runtime_spawn_call(rows, **_spawn_kwargs(model, effort))

        assert call["model"] == "model-a"

    @_INHERITANCE_CASES
    def test_rejects_controls_from_other_turn(
        self, extra_arguments: dict[str, str], model: str | None, effort: str | None
    ) -> None:
        """Stale parent controls are rejected when the spawn explicitly identifies a different turn.

        Otherwise a portable reviewer can appear to inherit a model and effort from a prior turn, weakening the frozen
        dispatch provenance even when its child agrees with those stale values.
        """
        rows = _spawn_history(extra_arguments, "turn-2")

        with pytest.raises(ValueError, match="runtime-parent-controls-turn-mismatch:node-1"):
            _load_validator()._runtime_spawn_call(rows, **_spawn_kwargs(model, effort))


class TestResolveExecutionMode:
    """Execution mode resolution keeps explicit intent above ambient config and never selects an unpromoted route."""

    @pytest.mark.parametrize(
        ("flag", "environment", "read_promoted", "write_promoted", "expected"),
        [
            pytest.param(
                "--execution=serial",
                {"CODEX_RIG_EXECUTION": "parallel-read"},
                True,
                False,
                {
                    "effective_mode": "serial",
                    "requested_mode": "serial",
                    "source": "explicit",
                    "write_approval_required": False,
                },
                id="explicit-serial-beats-environment",
            ),
            pytest.param(
                None,
                {"CODEX_RIG_EXECUTION": "parallel-read"},
                True,
                False,
                {"effective_mode": "parallel-read"},
                id="environment-selects-promoted-read",
            ),
            pytest.param(
                None,
                {},
                False,
                False,
                {
                    "effective_mode": "serial",
                    "requested_mode": "auto",
                    "source": "shipped-default",
                    "write_approval_required": False,
                },
                id="shipped-default-is-serial",
            ),
            pytest.param(None, {}, True, True, {"effective_mode": "parallel-read"}, id="auto-selects-promoted-read"),
            pytest.param(
                None,
                {},
                False,
                True,
                {"requested_mode": "auto", "effective_mode": "serial", "write_approval_required": False},
                id="auto-ignores-write-only-promotion",
            ),
            pytest.param(
                "--execution=auto",
                {},
                False,
                False,
                {"requested_mode": "auto", "effective_mode": "serial"},
                id="explicit-auto-before-promotion",
            ),
            pytest.param(
                "--execution=auto",
                {},
                True,
                False,
                {"effective_mode": "parallel-read"},
                id="explicit-auto-after-read-promotion",
            ),
            pytest.param(
                "--execution=parallel-write",
                {"CODEX_RIG_EXECUTION": "auto"},
                True,
                True,
                {
                    "effective_mode": "parallel-write",
                    "requested_mode": "parallel-write",
                    "source": "explicit",
                    "write_approval_required": True,
                },
                id="parallel-write-requires-approval",
            ),
        ],
    )
    def test_resolves_mode(
        self,
        flag: str | None,
        environment: dict[str, str],
        read_promoted: bool,
        write_promoted: bool,
        expected: dict[str, object],
    ) -> None:
        """Flag, environment, and promotion state resolve to the documented mode and approval requirement.

        Explicit flags outrank the environment, the shipped default and ``auto`` stay serial until the portable read
        route is promoted, a write-only promotion never selects a route, and parallel-write always needs approval.
        """
        resolution = _load_validator().resolve_execution_mode(
            flag,
            environment=environment,
            read_parallel_promoted=read_promoted,
            write_parallel_promoted=write_promoted,
        )

        assert {key: resolution[key] for key in expected} == expected

    @pytest.mark.parametrize("mode", ["parallel-read", "parallel-write"])
    def test_rejects_unpromoted_parallel_requests(self, mode: str) -> None:
        """A flag cannot bypass consumer promotion.

        Requesting either parallel route while neither is promoted must raise ``<mode>-not-promoted``.
        """
        resolver = _load_validator().resolve_execution_mode

        with pytest.raises(ValueError, match=f"^{mode}-not-promoted$"):
            resolver(f"--execution={mode}", environment={}, read_parallel_promoted=False, write_parallel_promoted=False)


class TestValidateManifest:
    """Schema-v1 manifest validation derives the actual mode from joined substantive work and fails closed."""

    def test_reads_schema_v1_manifest(self, wave: ParallelWave) -> None:
        """Historical schema-v1 manifests stay readable through the public validator.

        A valid two-node wave with overlapping intervals reports the parallel mode.
        """
        assert wave.validate()["actual_mode"] == "parallel"

    def test_derives_parallel_from_substantive_joined_nodes(self, wave: ParallelWave) -> None:
        """Overlapping, joined, substantive nodes produce the parallel summary.

        Prevents spawn-only or sequential fan-out from masquerading as parallel work.
        """
        summary = wave.validate()

        assert summary == {"acceptance_blocked": False, "actual_mode": "parallel", "integration_order": ["N1", "N2"]}

    def test_rejects_sequential_intervals_claiming_parallel(self, wave: ParallelWave) -> None:
        """A sequential second node cannot keep the parallel label even when both children completed.

        The second node starts after the first terminated, so the truthful-label check must reject the claim.
        """
        second = _nodes(wave.manifest)[1]
        second["attempts"][0]["start_event"] = _event("N2-start", 5)
        second["attempts"][0]["terminal_event"] = _event("N2-terminal", 6)
        second["join_event"] = _event("N2-join", 7)

        with pytest.raises(ValueError, match=r"^false-parallel-claim$"):
            wave.validate()

    @pytest.mark.parametrize(
        ("path", "value", "error"),
        [
            pytest.param((*_NODE, "role_card_sha256"), "0" * 64, "role-card-hash-mismatch:N1", id="role-hash"),
            pytest.param((*_NODE, "context_sha256"), "0" * 64, "context-hash-mismatch:N1", id="context-hash"),
            pytest.param(
                (*_NODE, "attempts", 0, "output_sha256"), "0" * 64, "output-hash-mismatch:N1", id="output-hash"
            ),
            pytest.param(
                (*_NODE, "join_event"), _event("N1-join", 4), "join-before-terminal:N1", id="join-before-terminal"
            ),
            pytest.param(
                ("stages", 0, "depends_on"), ["missing"], "stage-dependency-missing:S1", id="missing-stage-dependency"
            ),
            pytest.param(("configured_limit",), 5, "configured-limit-invalid", id="limit-above-ceiling"),
            pytest.param(
                (*_NODE, "resource_locks"), ["custom-lock"], "resource-lock-invalid:N1", id="unscoped-resource-lock"
            ),
            pytest.param(
                (*_NODE, "observed_controls", "enforced"),
                False,
                "capability-enforcement-required:N1",
                id="unproved-controls",
            ),
        ],
    )
    def test_rejects_mutated_field(
        self, wave: ParallelWave, path: tuple[str | int, ...], value: object, error: str
    ) -> None:
        """Fail closed on a single tampered or invalid field in an otherwise valid parallel manifest.

        Starting from a valid two-node wave, one field is replaced: role, context, or output hashes and an early join
        event break the exact role, context, output, and parent-join provenance; a missing stage dependency would let a
        stage start outside its barrier; a limit of five exceeds the four-child safety ceiling; an unscoped custom lock
        cannot be compared consistently; and requested-only sandbox controls cannot satisfy least privilege.
        """
        _set_path(wave.manifest, path, value)

        with pytest.raises(ValueError, match=f"^{error}$"):
            wave.validate()

    @pytest.mark.parametrize(
        ("depends_on", "wave_id", "start", "terminal", "join", "error"),
        [
            pytest.param(
                [], "wave-beta", 2, 5, 8, "stage-barrier-required:S2", id="independent-stage-bypasses-barrier"
            ),
            pytest.param(["S1"], "wave-alpha", 8, 9, 10, "wave-id-duplicate:wave-alpha", id="reused-wave-id"),
        ],
    )
    def test_second_stage_respects_barrier_and_unique_wave_id(
        self, wave: ParallelWave, depends_on: list[str], wave_id: str, start: int, terminal: int, join: int, error: str
    ) -> None:
        """Reject a second stage that escapes the serial barrier or reuses an earlier wave identifier.

        An independent stage overlapping the first would escape ownership, lock, and concurrency checks; a stage that
        reuses a wave identifier would make event and join evidence ambiguous across serial stages.
        """
        node = _completed_node(
            wave.run_dir,
            wave.roles_dir,
            node_id="N3",
            role_id="qa-specialist-secondary",
            start=start,
            terminal=terminal,
            join=join,
        )
        wave.manifest["stages"].append(
            {"stage_id": "S2", "depends_on": depends_on, "wave_id": wave_id, "nodes": [node]}
        )

        with pytest.raises(ValueError, match=f"^{error}$"):
            wave.validate()

    @pytest.mark.parametrize(
        ("paths", "locks"),
        [
            pytest.param(("src/shared.py", "src/shared.py"), ("git-index",), id="shared-path-and-lock"),
            pytest.param(("src\\shared.py", "src/shared.py"), (), id="windows-and-posix-alias"),
        ],
    )
    def test_rejects_overlapping_write_ownership(
        self, wave: ParallelWave, paths: tuple[str, str], locks: tuple[str, ...]
    ) -> None:
        """Two write owners cannot race on an alias, an ancestor, or a shared lock.

        Both nodes own the same file, either literally with a shared resource lock, or through native-Windows versus
        POSIX separators that must normalize to one path.
        """
        wave.manifest["write_approval"] = _valid_write_approval(wave.manifest)
        _make_write_nodes(wave.manifest, paths, locks)

        with pytest.raises(ValueError, match=r"^write-ownership-overlap:N1:N2$"):
            wave.validate()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("plan_sha256", "0" * 64, id="mismatched-plan-digest"),
            pytest.param("response", "deny", id="denied-response"),
            pytest.param("source", "environment", id="noninteractive-source"),
        ],
    )
    def test_rejects_serial_write_with_invalid_approval(self, wave: ParallelWave, field: str, value: str) -> None:
        """Reject a serial-write approval whose frozen plan digest, response, or source differs.

        Each case flips exactly one field of an otherwise exact approval.
        """
        manifest = _serial_write_wave(wave)
        manifest["write_approval"] = {**_valid_write_approval(manifest), field: value}

        with pytest.raises(ValueError, match=r"^write-approval-invalid$"):
            wave.validate()

    def test_rejects_serial_write_without_approval(self, wave: ParallelWave) -> None:
        """A serial write plan needs explicit approval before it is accepted.

        The manifest carries no ``write_approval`` at all.
        """
        _serial_write_wave(wave)

        with pytest.raises(ValueError, match=r"^write-approval-required$"):
            wave.validate()

    def test_accepts_serial_write_with_exact_approval(self, wave: ParallelWave) -> None:
        """The exact frozen-plan approval admits the serial write.

        The same manifest that is rejected without approval validates once the approval is attached.
        """
        manifest = _serial_write_wave(wave)
        manifest["write_approval"] = _valid_write_approval(manifest)

        assert wave.validate()["actual_mode"] == "serial"

    def test_rejects_secret_material_in_context_pack(self, wave: ParallelWave) -> None:
        """A hash-valid context pack cannot persist obvious credential material.

        The first node's context is rewritten with a synthetic private-key header and its digest refreshed, so only the
        content scan can reject it.
        """
        first = _nodes(wave.manifest)[0]
        context = wave.run_dir / str(first["context_path"])
        context.write_text("-----BEGIN " + "PRIVATE KEY-----\nsecret\n", encoding="utf-8", newline="\n")
        first["context_sha256"] = _sha256(context)

        with pytest.raises(ValueError, match=r"^context-sensitive-material:N1$"):
            wave.validate()

    def test_rejects_retry_of_deterministic_failure(self, wave: ParallelWave) -> None:
        """A deterministic finding or validation failure cannot be retried away.

        Attempt one failed with ``deterministic`` and attempt two completed; only transient errors may retry.
        """
        first = _nodes(wave.manifest)[0]
        completed = first["attempts"][0]
        first["attempts"] = [
            _failed_attempt("deterministic"),
            {**completed, "attempt": 2, "start_event": _event("N1-a2-start", 3)},
        ]
        first["selected_attempt"] = 2

        with pytest.raises(ValueError, match=r"^invalid-retry:N1$"):
            wave.validate()

    def test_accepts_one_transient_retry(self, wave: ParallelWave) -> None:
        """One named transient failure may retry while the selected output and events stay bound.

        Attempt one timed out; attempt two completed with its own start and terminal events and a later join.
        """
        first = _nodes(wave.manifest)[0]
        completed = first["attempts"][0]
        retried = {
            **completed,
            "attempt": 2,
            "start_event": _event("N1-a2-start", 3),
            "terminal_event": _event("N1-a2-terminal", 5),
        }
        first["attempts"] = [_failed_attempt("timeout"), retried]
        first["selected_attempt"] = 2
        first["join_event"] = _event("N1-join", 6)

        assert wave.validate()["actual_mode"] == "parallel"

    def test_cancel_requested_blocks_acceptance(self, wave: ParallelWave) -> None:
        """A cancel-requested child without a join blocks acceptance instead of failing validation.

        The non-terminal child is reported through ``acceptance_blocked`` so the run is neither accepted nor invalid.
        """
        _cancel_requested_node(wave, join_event=None)

        assert wave.validate()["acceptance_blocked"] is True

    def test_rejects_join_of_cancel_requested_child(self, wave: ParallelWave) -> None:
        """A non-terminal cancelled child cannot produce a false barrier join.

        The cancel-requested attempt has no terminal event, yet the manifest records a join for it.
        """
        _cancel_requested_node(wave, join_event=_event("N1-join", 6))

        with pytest.raises(ValueError, match=r"^cancel-requested-joined:N1$"):
            wave.validate()


class TestReadOnlyRuntime:
    """Schema-v2 runtime validation binds the manifest to plan, parent, and child host records."""

    def test_binds_plan_lineage_terminal_output_and_declared_controls(self, v2_run: RuntimeRun) -> None:
        """Runtime evidence, not self-attested manifest events, establishes the parallel summary.

        A valid portable run yields the restricted-network evidence level, bound manifest digest, token-budget
        admission, sanitized output, and the parent-join digest of each child's final answer; no network guarantee is
        claimed and no consumer is promoted without an identified consumer.
        """
        summary = v2_run.validate()

        assert summary["actual_mode"] == "parallel"
        assert summary["evidence_level"] == "portable-read-restricted"
        assert summary["network_mode"] == "restricted"
        assert summary["approval_policy"] == "never"
        assert "network_guarantee" not in summary
        assert summary["filesystem_credential_isolation"] == "unverified"
        assert summary["write_parallel_eligible"] is False
        assert summary["integration_order"] == ["N1", "N2"]
        assert summary["manifest_sha256"] == _sha256(v2_run.manifest_path)
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
        ("mutate", "error"),
        [
            pytest.param(
                lambda run, rows, join: _set_path(join, ("payload", "id"), "different-id"),
                "runtime-parent-join-missing:N1",
                id="missing",
            ),
            pytest.param(
                lambda run, rows, join: rows.append(copy.deepcopy(join)),
                "runtime-parent-join-duplicate:N1",
                id="duplicate",
            ),
            pytest.param(
                lambda run, rows, join: _set_path(join, ("payload", "author"), "/root/unrelated"),
                "runtime-parent-join-sender-mismatch:N1",
                id="wrong-sender",
            ),
            pytest.param(
                lambda run, rows, join: _set_path(join, ("payload", "content", 0, "text"), "wrong final answer"),
                "runtime-parent-join-content-mismatch:N1",
                id="wrong-content",
            ),
            pytest.param(
                lambda run, rows, join: _set_path(join, ("payload", "recipient"), ""),
                "runtime-parent-join-recipient-required:N1",
                id="empty-recipient",
            ),
            pytest.param(
                lambda run, rows, join: _set_path(join, ("timestamp",), "2026-08-24T12:00:00Z"),
                "runtime-parent-join-before-terminal:N1",
                id="early",
            ),
            pytest.param(
                lambda run, rows, join: _set_path(
                    run.manifest, (*_NODE, "join_event"), _event("self-attested-only", 6)
                ),
                "runtime-parent-join-missing:N1",
                id="self-attested",
            ),
        ],
    )
    def test_binds_each_join_to_one_consumed_final_answer(
        self, v2_run: RuntimeRun, mutate: Callable[..., object], error: str
    ) -> None:
        """Reject self-attested, malformed, or temporally impossible parent joins.

        The parent rollout must hold exactly one final-answer message per recorded join, sent by the right child to the
        parent with matching content after the child's terminal event; each case breaks one of those properties.
        """
        with _editing(v2_run.parent_rollout) as rows:
            mutate(v2_run, rows, _only(rows, payload_type="agent_message", payload_id="N1-join"))
        v2_run.save_manifest()

        with pytest.raises(ValueError, match=f"^{error}$"):
            v2_run.validate()

    def test_rejects_plan_bytes_that_differ_from_approved_digest(self, v2_run: RuntimeRun) -> None:
        """Runtime acceptance is bound to the exact frozen plan bytes.

        The plan file is overwritten after its digest was recorded in the manifest.
        """
        v2_run.plan_path.write_text('{"run_id":"changed"}\n', encoding="utf-8", newline="\n")

        with pytest.raises(ValueError, match=r"^runtime-plan-hash-mismatch$"):
            v2_run.validate()

    def test_rejects_child_without_terminal_event(self, v2_run: RuntimeRun) -> None:
        """A parent activity or wait response cannot masquerade as child completion.

        The second child's rollout loses its ``task_complete`` row, leaving only parent-side evidence of completion.
        """
        rollout = v2_run.child_rollout(2)
        rollout_rows = [row for row in read_jsonl(rollout) if row.get("payload", {}).get("type") != "task_complete"]
        _write_jsonl(rollout, rollout_rows)

        with pytest.raises(ValueError, match=r"^runtime-child-terminal-missing:N2$"):
            v2_run.validate()

    def test_rejects_rollout_schema_drift(self, v2_run: RuntimeRun) -> None:
        """Fail closed when an internal host record changes shape.

        The first child's start activity is renamed to an unknown item type, so its start can no longer be matched.
        """
        with _editing(v2_run.parent_rollout) as rows:
            _assign(rows, ("payload", "item", "type"), "FutureAgentActivity", item_id="activity-start-1")

        with pytest.raises(ValueError, match=r"^runtime-parent-start-missing:N1$"):
            v2_run.validate()

    def test_rejects_model_or_effort_that_differs_from_spawn_selection(self, v2_run: RuntimeRun) -> None:
        """A correctly linked child cannot silently run with another spawn selection.

        The first child's settings and turn context both report a model the spawn never selected.
        """
        with _editing(v2_run.child_rollout(1)) as rows:
            _assign(
                rows,
                ("payload", "thread_settings", "model"),
                "unexpected-model",
                payload_type="thread_settings_applied",
            )
            _assign(rows, ("payload", "model"), "unexpected-model", row_type="turn_context")

        with pytest.raises(ValueError, match=r"^runtime-spawn-model-effort-mismatch:N1$"):
            v2_run.validate()

    def test_preserves_truthful_serial_fallback_label(self, v2_run: RuntimeRun) -> None:
        """An equal-gate fallback keeps its own label instead of reading as ordinary independent spawning.

        The second child runs after the first one finished and the manifest claims ``serial-fallback``.
        """
        v2_run.manifest["claimed_mode"] = "serial-fallback"
        with _editing(v2_run.child_rollout(2)) as rows:
            terminal = _only(rows, payload_type="task_complete")["payload"]
            terminal.update(started_at=1_800_000_020, completed_at=1_800_000_025, duration_ms=5000)
        v2_run.save_manifest()

        assert v2_run.validate()["actual_mode"] == "serial-fallback"

    def test_rejects_schema_v1_runtime_validation(self, tmp_path: Path) -> None:
        """Schema-v1 manifests are unsupported for runtime validation.

        Historical manifests stay readable through the manifest validator, but cannot claim runtime evidence.
        """
        run = _runtime_fixture(tmp_path)

        with pytest.raises(ValueError, match=r"^runtime-schema-unsupported$"):
            run.validate()

    @pytest.mark.parametrize("consumer_id", _CONSUMER_IDS)
    @pytest.mark.parametrize(("selector", "profile_path"), _CONTROL_RECORDS)
    def test_rejects_write_grant_behind_read_only_label(
        self,
        v2_run: RuntimeRun,
        tmp_path: Path,
        consumer_id: str,
        selector: dict[str, str],
        profile_path: tuple[str, ...],
    ) -> None:
        """A write grant hidden behind a read-only label is rejected in either host record.

        The first child's thread settings or turn context grants write access to a directory while the sandbox label
        still reads read-only.
        """
        _bind_portable_read_consumer_policy(v2_run, consumer_id=consumer_id)
        _grant_child_access(v2_run, tmp_path, selector, profile_path, "write")

        with pytest.raises(ValueError, match=r"^runtime-child-filesystem-write-grant:N1$"):
            v2_run.validate(expected_consumer_id=consumer_id)

    @pytest.mark.parametrize("consumer_id", _CONSUMER_IDS)
    @pytest.mark.parametrize(("selector", "profile_path"), _CONTROL_RECORDS)
    def test_accepts_read_grant_without_isolation_claim(
        self,
        v2_run: RuntimeRun,
        tmp_path: Path,
        consumer_id: str,
        selector: dict[str, str],
        profile_path: tuple[str, ...],
    ) -> None:
        """A read-only filesystem grant is accepted and promotes the consumer without claiming isolation.

        Same arrangement as the write-grant case with ``read`` access, which must stay admissible.
        """
        _bind_portable_read_consumer_policy(v2_run, consumer_id=consumer_id)
        _grant_child_access(v2_run, tmp_path, selector, profile_path, "read")

        summary = v2_run.validate(expected_consumer_id=consumer_id)

        assert summary["runtime_promotion_eligible"] is True
        assert summary["filesystem_credential_isolation"] == "unverified"

    @pytest.mark.parametrize(
        "filesystem",
        [
            pytest.param({"type": "unrestricted"}, id="unrestricted"),
            pytest.param({"type": "restricted", "entries": None}, id="missing-entries"),
            pytest.param({"type": "restricted", "entries": ["read"]}, id="malformed-entry"),
            pytest.param({"type": "restricted", "entries": [{"access": "unknown"}]}, id="unknown-access"),
        ],
    )
    def test_rejects_unrecognized_filesystem_controls(self, v2_run: RuntimeRun, filesystem: dict[str, Any]) -> None:
        """Fail closed on explicit filesystem records outside the supported read-only shape.

        The first child's turn context declares an unrestricted, entry-less, malformed, or unknown-access profile.
        """
        with _editing(v2_run.child_rollout(1)) as rows:
            _assign(rows, ("payload", "permission_profile", "file_system"), filesystem, row_type="turn_context")

        with pytest.raises(ValueError, match=r"^runtime-child-filesystem-controls-invalid:N1$"):
            v2_run.validate()

    def test_accepts_subsecond_duration_precision_residual(self, v2_run: RuntimeRun) -> None:
        """A millisecond duration consistent with whole-second terminal endpoints is accepted.

        The first child's duration is 632 ms longer than its whole-second endpoints, which is rounding residue.
        """
        with _editing(v2_run.child_rollout(1)) as rows:
            _only(rows, payload_type="task_complete")["payload"]["duration_ms"] += 632

        assert v2_run.validate()["actual_mode"] == "parallel"

    def test_rejects_duration_outside_endpoint_precision(self, v2_run: RuntimeRun) -> None:
        """A terminal duration inconsistent with whole-second endpoint precision is rejected.

        The first child's duration is a full second longer than its endpoints allow.
        """
        with _editing(v2_run.child_rollout(1)) as rows:
            _only(rows, payload_type="task_complete")["payload"]["duration_ms"] += 1000

        with pytest.raises(ValueError, match=r"^runtime-child-terminal-invalid:N1$"):
            v2_run.validate()

    def test_requires_join_delivery_to_authoritative_parent(self, v2_run: RuntimeRun) -> None:
        """Exact child delivery addressed to a path other than the parent is rejected.

        The first join keeps its content but names ``/root/not-parent`` as recipient and task name.
        """
        with _editing(v2_run.parent_rollout) as rows:
            join = _only(rows, payload_type="agent_message", payload_id="N1-join")["payload"]
            join["recipient"] = "/root/not-parent"
            join["content"][0]["text"] = join["content"][0]["text"].replace(
                "Task name: /root", "Task name: /root/not-parent"
            )

        with pytest.raises(ValueError, match=r"^runtime-parent-join-recipient-mismatch:N1$"):
            v2_run.validate()

    @pytest.mark.parametrize(
        ("plan", "error"),
        [
            pytest.param(
                {"run_id": "run-1", "capability_policy": {"task_sensitivity": "sensitive"}},
                "runtime-plan-task-sensitivity-mismatch",
                id="sensitive-task",
            ),
            pytest.param({"run_id": "run-1"}, "runtime-plan-capability-policy-missing", id="missing-capability-policy"),
        ],
    )
    def test_portable_tier_requires_parent_plan_task_sensitivity(
        self, v2_run: RuntimeRun, plan: dict[str, Any], error: str
    ) -> None:
        """Portable eligibility comes from the frozen parent-owned sensitivity policy, not manifest self-attestation.

        The plan is replaced (and the manifest digest rebound) by one declaring a sensitive task or no policy at all.
        """
        v2_run.save_plan(plan)

        with pytest.raises(ValueError, match=f"^{error}$"):
            v2_run.validate()

    @pytest.mark.parametrize(
        ("mutate", "error"),
        [
            pytest.param(
                lambda plan: plan.pop("token_budgets"), "runtime-plan-token-budgets-missing", id="missing-budgets"
            ),
            pytest.param(
                _assigning((("token_budgets", 0, "ceiling_tokens"), 700)),
                "runtime-token-budget-dispatch-exceeds-admission:wave-alpha",
                id="overflow",
            ),
            pytest.param(
                _assigning((("token_budgets", 0, "node_order"), ["N2", "N1"])),
                "runtime-plan-token-budget-node-mismatch:wave-alpha",
                id="node-mismatch",
            ),
        ],
    )
    def test_binds_every_spawned_node_to_frozen_token_budget(
        self, v2_run: RuntimeRun, mutate: Callable[[Any], object], error: str
    ) -> None:
        """Runtime acceptance is rejected when frozen wave reservations did not admit every child.

        The plan loses its budgets, shrinks the ceiling below the two 400-token reservations, or reorders the nodes.
        """
        plan = v2_run.read_plan()
        mutate(plan)
        v2_run.save_plan(plan)

        with pytest.raises(ValueError, match=f"^{error}$"):
            v2_run.validate()

    def test_historical_reader_preserves_unbudgeted_evidence_without_promotion(self, v2_run: RuntimeRun) -> None:
        """Earlier unbudgeted schema-v2 evidence stays readable without reopening admission.

        With the plan's budgets removed and the historical flag set, acceptance is blocked and no promotion is granted.
        """
        plan = v2_run.read_plan()
        del plan["token_budgets"]
        v2_run.save_plan(plan)

        summary = v2_run.validate(historical_unbudgeted=True)

        assert summary["acceptance_blocked"] is True
        assert summary["runtime_promotion_eligible"] is False
        assert summary["token_budget_admissions"] == []
        assert summary["evidence_level"] == "historical-portable-read-restricted-unbudgeted"

    @pytest.mark.parametrize(
        ("path", "value", "error"),
        [
            pytest.param((*_NODE, "mutation"), "write", "runtime-write-parallel-unsupported:N1", id="write-node"),
            pytest.param(
                ("claimed_mode",), "serial", "runtime-mode-claim-mismatch:serial:parallel", id="false-serial-claim"
            ),
            pytest.param(
                (*_NODE, "observed_controls", "network"),
                False,
                "runtime-node-capability-mismatch:N1",
                id="legacy-false-node-network",
            ),
            pytest.param(
                (*_EVIDENCE, "network", "approval_policy"),
                "on-request",
                "portable-network-approval-policy-invalid",
                id="on-request-network-approval",
            ),
            pytest.param((*_EVIDENCE, "unexpected"), "field", "capability-evidence-invalid", id="malformed"),
            pytest.param(
                (*_EVIDENCE, "network", "external_events"),
                ["browser.open"],
                "portable-external-events-invalid",
                id="declared-external-event",
            ),
            pytest.param(
                (*_EVIDENCE, "credentials", "context_scan"),
                "failed",
                "portable-context-scan-required",
                id="context-scan-failed",
            ),
            pytest.param(
                (*_EVIDENCE, "credentials", "filesystem_isolation"),
                "isolated",
                "portable-filesystem-isolation-invalid",
                id="filesystem-isolation-claimed",
            ),
            pytest.param((*_EVIDENCE, "task_sensitivity"), "sensitive", "portable-sensitive-task", id="sensitive-task"),
            pytest.param(
                (*_EVIDENCE, "tier"),
                "host-isolated",
                "host-isolation-evidence-unavailable",
                id="unobserved-host-isolation",
            ),
        ],
    )
    def test_rejects_untrusted_manifest_claims(
        self, v2_run: RuntimeRun, path: tuple[str | int, ...], value: object, error: str
    ) -> None:
        """A manifest self-attestation cannot expand the portable-read-restricted scope.

        A valid portable schema-v2 run has one manifest field replaced. Declared thread controls must not authorize a
        write-capable parallel node, a planned serial label must not conceal observed parallel work, and legacy false
        denial records must not earn a restricted promotion. Approval-gated network access, malformed or unsafe
        capability evidence, sensitive tasks, and an unobserved stronger host-isolation tier must each be rejected.
        """
        _set_path(v2_run.manifest, path, value)
        v2_run.save_manifest()

        with pytest.raises(ValueError, match=f"^{error}$"):
            v2_run.validate()

    def test_portable_tier_rejects_mixed_host_network_records(self, v2_run: RuntimeRun) -> None:
        """Every child host record must support the same bounded capability claim.

        The second child's settings and turn context report network ``False`` while the manifest claims restricted.
        """
        with _editing(v2_run.child_rollout(2)) as rows:
            settings = {"row_type": "event_msg", "payload_type": "thread_settings_applied"}
            _assign(rows, ("payload", "thread_settings", "permission_profile", "network"), False, **settings)
            _assign(rows, ("payload", "permission_profile", "network"), False, row_type="turn_context")

        with pytest.raises(ValueError, match=r"^portable-network-record-mismatch:N2$"):
            v2_run.validate()

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"type": "function_call", "name": "browser.open"}, id="browser-call"),
            pytest.param({"type": "future_tool_call"}, id="unknown-future-call"),
        ],
    )
    def test_portable_tier_rejects_external_network_calls(self, v2_run: RuntimeRun, payload: dict[str, str]) -> None:
        """Browser, search, MCP, connector, and unknown future calls are rejected from local-only waves.

        A named browser call and an unrecognised response-item call must both trip the network gate, so a future host
        call type cannot bypass it silently.
        """
        _append_child_row(v2_run, payload)

        with pytest.raises(ValueError, match=r"^portable-external-network-event$"):
            v2_run.validate()

    def test_portable_tier_rejects_secret_material_in_bound_output_and_terminal(self, v2_run: RuntimeRun) -> None:
        """A hash-valid child result cannot persist a common credential pattern.

        The first child's output file, terminal message, and parent join all carry a token-shaped secret, with digests
        refreshed so only the content scan can reject it.
        """
        secret = "ghp_abcdefghijklmnopqrst"  # noqa: S105 - fake credential fixture for redaction test
        attempt = _nodes(v2_run.manifest)[0]["attempts"][0]
        output_path = v2_run.manifest_path.parent / str(attempt["output_path"])
        output_path.write_text(secret + "\n", encoding="utf-8", newline="\n")
        attempt["output_sha256"] = _sha256(output_path)
        with _editing(v2_run.child_rollout(1)) as rows:
            _assign(rows, ("payload", "last_agent_message"), secret, payload_type="task_complete")
        join_text = "\n".join(
            ("Message Type: FINAL_ANSWER", "Task name: /root", "Sender: /root/runtime_n1", "Payload:", secret)
        )
        with _editing(v2_run.parent_rollout) as rows:
            _assign(
                rows, ("payload", "content", 0, "text"), join_text, payload_type="agent_message", payload_id="N1-join"
            )
        v2_run.save_manifest()

        with pytest.raises(ValueError, match=r"^runtime-output-sensitive-material:N1$"):
            v2_run.validate()

    def test_portable_tier_reports_configuration_not_exec_network_observation(self, v2_run: RuntimeRun) -> None:
        """Local command arguments are not misreported as proof that no network attempt occurred.

        A child exec call whose command line mentions curl and ssh keeps the configuration-based restricted claim and
        asserts no network guarantee.
        """
        command = {"cmd": "curl https://example.invalid && ssh user@example.invalid"}
        _append_child_row(v2_run, {"type": "function_call", "name": "exec", "arguments": json.dumps(command)})

        summary = v2_run.validate()

        assert summary["evidence_level"] == "portable-read-restricted"
        assert summary["network_mode"] == "restricted"
        assert summary["approval_policy"] == "never"
        assert "network_guarantee" not in summary

    @pytest.mark.integration
    def test_accepts_nested_parent_without_inherited_spawn_receipts(self, v2_run: RuntimeRun) -> None:
        """A real runtime consumer binds only the nested owner's spawn and join evidence.

        The parent rollout is wrapped in nested history that also replays the original spawn and join rows as inherited
        events; validation must ignore the replay and still accept the run.
        """
        rows = read_jsonl(v2_run.parent_rollout)
        nested = _nested_parent_history(rows)
        inherited = [dict(row) for row in rows[1:]]
        nested = nested[:4] + inherited + nested[4:]
        nested[0]["payload"]["subagent_history_start_ordinal"] = 4 + len(inherited)
        _write_jsonl(v2_run.parent_rollout, [{**row, "ordinal": index} for index, row in enumerate(nested)])

        assert v2_run.validate()["actual_mode"] == "parallel"


class TestPortableReadConsumerRuntimeMatrix:
    """Each promoted consumer is bound to the portable read-only runtime contract."""

    @pytest.mark.parametrize(
        ("skill", "safe_surface", "serial_surface", "join_clause", "resource_clause", "write_disabled"),
        [
            pytest.param(
                "implement",
                "read-only evidence, acceptance, and documentation-impact passes",
                "implementation, test, and documentation writes",
                "join every terminal handoff before implementation, integration, gates, or acceptance",
                "Shared paths, indexes, caches, generated outputs, test environments, ports, devices, or undeclared resources force serial execution or re-planning.",
                "Generic portable parallel writes remain disabled.",
                id="implement",
            ),
            pytest.param(
                "manage",
                "read-only inventory, reference, ownership, and policy-impact scans",
                "create, update, delete, rename, and permission mutations",
                "join every terminal scan before edits, propagation, gates, or acceptance",
                "Shared targets, paths, indexes, caches, generated outputs, test environments, ports, devices, or undeclared resources force serial execution or re-planning.",
                "Generic parallel writes remain disabled.",
                id="manage",
            ),
        ],
    )
    def test_documents_portable_read_only_contract(
        self,
        skill: str,
        safe_surface: str,
        serial_surface: str,
        join_clause: str,
        resource_clause: str,
        write_disabled: str,
    ) -> None:
        """Each promoted consumer's documented boundary is complete and fails closed.

        The contract spans the consumer's own Parallel Adoption section, which carries its consumer values, and the
        shared ``portable-parallel-read.md`` fragment, which carries the invariant route text. The skill must point at
        the fragment, so a consumer cannot lose the invariant half while this test still passes. Text is compared with
        whitespace collapsed, so reflowing prose never breaks it.
        """
        text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
        skill_section = _squash(_section_body(text, "## Parallel Adoption (Portable read-only)"))
        fragment = _squash((PLUGIN_ROOT / "shared" / "portable-parallel-read.md").read_text(encoding="utf-8"))
        section = skill_section + " " + fragment
        contracts = [
            *_PORTABLE_SUBSECTIONS,
            *_PORTABLE_CONTRACTS,
            safe_surface,
            serial_surface,
            join_clause,
            resource_clause,
            write_disabled,
            f"parallel_execution.py preflight --consumer {skill}",
            f"parallel_execution.py validate-runtime --consumer {skill}",
        ]

        assert '"execution": "optional auto|serial|parallel-read|parallel-write; default auto"' in _squash(text)
        assert "read [portable-parallel-read.md](../../shared/portable-parallel-read.md)" in skill_section
        assert [contract for contract in contracts if contract not in section] == []
        assert [exception for exception in _PORTABLE_UNSAFE_EXCEPTIONS if exception in section] == []

    @pytest.mark.parametrize("skill", ["implement", "manage"])
    def test_accepts_promoted_complete_join(self, v2_run: RuntimeRun, skill: str) -> None:
        """Complete read-only runtime evidence is accepted for each promoted consumer.

        Every child has a terminal event and a parent join, and the plan promotes the consumer under test.
        """
        _bind_portable_read_consumer_policy(v2_run, consumer_id=skill)

        summary = v2_run.validate(expected_consumer_id=skill)

        assert summary["runtime_promotion_eligible"] is True
        assert summary["write_parallel_eligible"] is False
        assert summary["consumer_id"] == skill

    @pytest.mark.parametrize("skill", ["implement", "manage"])
    def test_rejects_missing_parent_join(self, v2_run: RuntimeRun, skill: str) -> None:
        """A promoted consumer cannot proceed when the first child's parent join is missing.

        The join message is renamed in the parent rollout so no delivery matches the recorded join event.
        """
        _bind_portable_read_consumer_policy(v2_run, consumer_id=skill)
        with _editing(v2_run.parent_rollout) as rows:
            _assign(rows, ("payload", "id"), "missing-join", payload_type="agent_message", payload_id="N1-join")

        with pytest.raises(ValueError, match=r"^runtime-parent-join-missing:N1$"):
            v2_run.validate(expected_consumer_id=skill)

    @pytest.mark.parametrize("skill", ["implement", "manage"])
    def test_rejects_write_capable_node(self, v2_run: RuntimeRun, skill: str) -> None:
        """A promoted portable-read consumer cannot dispatch a write-capable child.

        The first node is marked as a write mutation, which the read-only route does not support.
        """
        _bind_portable_read_consumer_policy(v2_run, consumer_id=skill)
        _set_path(v2_run.manifest, (*_NODE, "mutation"), "write")
        v2_run.save_manifest()

        with pytest.raises(ValueError, match=r"^runtime-write-parallel-unsupported:N1$"):
            v2_run.validate(expected_consumer_id=skill)


class TestConsumerPreflight:
    """The consumer preflight and runtime commands derive promotion and write authority from the frozen plan."""

    @pytest.mark.installed_plugin
    @pytest.mark.parametrize("consumer_id", ["implement", "manage"])
    def test_cli_binds_auto_and_exact_parent_write_approval(self, v2_run: RuntimeRun, consumer_id: str) -> None:
        """The shipped consumer command derives promotion and exact parent write authority.

        Runs the real validator script with ``--execution=auto`` against a plan promising planned parent writes and a
        matching approval; the JSON result must pick the parallel-read route and report the approval as validated.
        """
        _bind_portable_read_consumer_policy(v2_run, consumer_id=consumer_id, parent_writes="planned")
        approval_path = _write_approval(v2_run.plan_path, source="user-prompt")

        completed = _run_validator_cli(
            "preflight",
            "--consumer",
            consumer_id,
            "--plan",
            str(v2_run.plan_path),
            "--approval",
            str(approval_path),
            "--execution=auto",
        )

        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["consumer_id"] == consumer_id
        assert result["requested_mode"] == "auto"
        assert result["effective_mode"] == "parallel-read"
        assert result["write_approval_required"] is True
        assert result["write_approval_validated"] is True
        assert result["plan_sha256"] == _sha256(v2_run.plan_path)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("plan_sha256", "0" * 64, id="plan_sha256"),
            pytest.param("response", "deny", id="response"),
            pytest.param("source", "environment", id="source"),
        ],
    )
    @pytest.mark.parametrize(
        ("execution", "host_approval"),
        [
            pytest.param("--execution=auto", "never", id="parallel-read"),
            pytest.param("--execution=serial", "never", id="serial"),
            pytest.param("--execution=auto", "on-request", id="serial-fallback"),
        ],
    )
    def test_rejects_each_invalid_write_approval_field(
        self, v2_run: RuntimeRun, execution: str, host_approval: str, field: str, value: str
    ) -> None:
        """Stale, denied, or non-human parent write authority is rejected independently on every route.

        The plan promises parent writes; the approval differs from the exact one in one field, and the parallel-read,
        explicit serial, and host-forced serial-fallback routes must each refuse it.
        """
        _bind_portable_read_consumer_policy(
            v2_run, consumer_id="implement", parent_writes="planned", host_approval_policy=host_approval
        )
        approval_path = _write_approval(v2_run.plan_path, **{field: value})

        with pytest.raises(ValueError, match=r"^write-approval-invalid$"):
            _resolve_consumer(v2_run.plan_path, "implement", execution, approval_path)

    @pytest.mark.parametrize("consumer_id", ["implement", "manage"])
    @pytest.mark.parametrize(_SERIAL_ROUTE_FIELDS, _SERIAL_ROUTES)
    def test_admits_serial_parent_writes_without_new_approval(
        self,
        v2_run: RuntimeRun,
        consumer_id: str,
        execution: str,
        host_approval: str,
        requested: str,
        fallback: str | None,
    ) -> None:
        """Serial parent writes use the existing task authorization when no approval is supplied.

        Both an explicit serial request and the host-forced serial fallback resolve to serial without demanding the
        exact-plan approval that only the parallel-read route needs.
        """
        _bind_portable_read_consumer_policy(
            v2_run, consumer_id=consumer_id, parent_writes="planned", host_approval_policy=host_approval
        )

        result = _resolve_consumer(v2_run.plan_path, consumer_id, execution, None)

        expected = {
            "effective_mode": "serial",
            "requested_mode": requested,
            "write_approval_required": False,
            "write_approval_validated": False,
            "plan_sha256": _sha256(v2_run.plan_path),
        }
        assert {key: result[key] for key in expected} == expected
        assert result.get("fallback_reason") == fallback

    @pytest.mark.parametrize("consumer_id", ["implement", "manage"])
    @pytest.mark.parametrize(_SERIAL_ROUTE_FIELDS, _SERIAL_ROUTES)
    def test_validates_supplied_approval_on_serial_routes(
        self,
        v2_run: RuntimeRun,
        consumer_id: str,
        execution: str,
        host_approval: str,
        requested: str,
        fallback: str | None,
    ) -> None:
        """A supplied approval is validated on serial routes even though it is not required.

        Same arrangement as the approval-free case plus a matching approval file, which must be reported as validated.
        """
        _bind_portable_read_consumer_policy(
            v2_run, consumer_id=consumer_id, parent_writes="planned", host_approval_policy=host_approval
        )
        approval_path = _write_approval(v2_run.plan_path, source="user-prompt")

        result = _resolve_consumer(v2_run.plan_path, consumer_id, execution, approval_path)

        expected = {
            "effective_mode": "serial",
            "requested_mode": requested,
            "write_approval_required": False,
            "write_approval_validated": True,
            "plan_sha256": _sha256(v2_run.plan_path),
        }
        assert {key: result[key] for key in expected} == expected
        assert result.get("fallback_reason") == fallback

    def test_requires_approval_for_planned_parent_writes(self, v2_run: RuntimeRun) -> None:
        """A promoted read wave cannot become an approval-free parent write.

        The plan promises parent writes and the caller supplies neither a flag nor an approval.
        """
        _bind_portable_read_consumer_policy(v2_run, consumer_id="manage", parent_writes="planned")

        with pytest.raises(ValueError, match=r"^write-approval-required$"):
            _resolve_consumer(v2_run.plan_path, "manage", None, None)

    @pytest.mark.parametrize("consumer_id", ["implement", "manage"])
    def test_runtime_cli_requires_identity_and_returns_bound_summary(
        self, v2_run: RuntimeRun, consumer_id: str
    ) -> None:
        """The shipped post-join consumer command validates the run and returns the bound summary.

        Runs the real validator script with ``validate-runtime --consumer``; the JSON summary must name the consumer and
        mark it promotion-eligible without write parallelism.
        """
        _bind_portable_read_consumer_policy(v2_run, consumer_id=consumer_id)

        completed = _run_validator_cli(
            "validate-runtime",
            "--consumer",
            consumer_id,
            "--manifest",
            str(v2_run.manifest_path),
            "--plan",
            str(v2_run.plan_path),
            "--parent-rollout",
            str(v2_run.parent_rollout),
            "--sessions-dir",
            str(v2_run.sessions_dir),
            "--run-dir",
            str(v2_run.manifest_path.parent),
            "--roles-dir",
            str(v2_run.roles_dir),
        )

        assert completed.returncode == 0, completed.stderr
        summary = json.loads(completed.stdout)
        assert summary["consumer_id"] == consumer_id
        assert summary["runtime_promotion_eligible"] is True
        assert summary["write_parallel_eligible"] is False

    @pytest.mark.parametrize("consumer_id", ["implement", "manage"])
    @pytest.mark.parametrize(
        ("field", "value", "error"),
        [
            pytest.param("consumer_id", "other", "runtime-consumer-id-mismatch", id="consumer_id"),
            pytest.param("capability", "parallel-write", "runtime-consumer-capability-invalid", id="capability"),
            pytest.param("promotion_status", "pending", "runtime-consumer-promotion-required", id="promotion_status"),
            pytest.param(
                "parent_mutations", "parallel", "runtime-consumer-parent-mutations-invalid", id="parent_mutations"
            ),
            pytest.param(
                "canonical_gates", "parallel", "runtime-consumer-canonical-gates-invalid", id="canonical_gates"
            ),
        ],
    )
    def test_runtime_requires_exact_promoted_consumer_policy(
        self, v2_run: RuntimeRun, consumer_id: str, field: str, value: str, error: str
    ) -> None:
        """A consumer plan that loosens portable-read-only parent boundaries is rejected.

        One field of the promoted consumer policy is replaced by a value that widens capability, defers promotion, or
        lets the parent mutate or gate in parallel.
        """
        _bind_portable_read_consumer_policy(v2_run, consumer_id=consumer_id)
        plan = v2_run.read_plan()
        plan["consumer_policy"][field] = value
        v2_run.save_plan(plan)

        with pytest.raises(ValueError, match=f"^{error}$"):
            v2_run.validate(expected_consumer_id=consumer_id)


class TestTokenBudget:
    """Wave token budgets admit a stable prefix of reservations and fail closed on incoherent state."""

    def test_admits_stable_prefix_and_preserves_existing_work(self) -> None:
        """Budget exhaustion neither replays completed work nor kills active children.

        With a 100-token ceiling, 50 tokens already completed or active leave room for exactly one more node; the last
        node is routed to a serial re-plan under the same gates.
        """
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
        [
            pytest.param({"ceiling_tokens": 0}, "token-budget-ceiling-invalid", id="ceiling-zero"),
            pytest.param({"node_order": "first"}, "token-budget-node-order-invalid", id="node-order-not-list"),
            pytest.param(
                {"reservations": ["first", "second"]}, "token-reservations-invalid", id="reservations-not-mapping"
            ),
            pytest.param(
                {"reservations": {"first": True, "second": 20}},
                "token-reservation-invalid:first",
                id="boolean-reservation",
            ),
            pytest.param(
                {"reservations": {"first": 20}}, "token-reservation-node-mismatch", id="reservation-node-mismatch"
            ),
            pytest.param(
                {"completed_node_ids": "first"}, "token-budget-completed-state-invalid", id="completed-not-list"
            ),
            pytest.param(
                {"active_node_ids": ["second"]}, "token-budget-existing-state-not-prefix", id="active-not-prefix"
            ),
            pytest.param(
                {"completed_node_ids": ["first"], "active_node_ids": ["first"]},
                "token-budget-state-overlap:first",
                id="completed-and-active-overlap",
            ),
            pytest.param(
                {"ceiling_tokens": 10, "completed_node_ids": ["first"]},
                "token-budget-already-exceeded",
                id="already-exceeded",
            ),
        ],
    )
    def test_rejects_unsafe_or_incoherent_admission_state(self, overrides: dict[str, object], error: str) -> None:
        """A frozen reservation budget that cannot prove bounded admission fails closed.

        A valid two-node budget is damaged one way at a time: a bad ceiling, order, reservation, or node state.
        """
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


class TestRollbackContract:
    """Published rollback guidance keeps identity evidence and quality gates intact."""

    @pytest.mark.parametrize(
        "document",
        [
            pytest.param(PLUGIN_ROOT / "README.md", id="readme"),
        ],
    )
    def test_preserves_identity_evidence_and_gates(self, document: Path) -> None:
        """Neither published rollback document authorizes weaker recovery.

        Each document must keep the four rollback clauses: disable the opt-in without changing the plan, preserve
        completed evidence and gates, serialize only unfinished work, and retain ambiguous worktrees. Text is compared
        with whitespace collapsed.
        """
        text = _squash(document.read_text(encoding="utf-8"))

        assert [clause for clause in _ROLLBACK_CLAUSES if clause not in text] == []


class TestReviewInputAdmission:
    """Review-input admissions exempt only the exact approved occurrence in the exact complete input."""

    @pytest.mark.parametrize(
        ("text", "admissions", "sensitive"),
        [
            pytest.param("ordinary context", [], False, id="ordinary-context"),
            pytest.param(_BEARER_TEXT, [], True, id="unadmitted-secret"),
            pytest.param(_BEARER_TEXT, [_BEARER_ADMISSION], False, id="exact-admission"),
            pytest.param(_BEARER_TEXT + "-changed", [_BEARER_ADMISSION], True, id="changed-context"),
            pytest.param(
                _BEARER_TEXT + " " + _BEARER_MATCH, [_BEARER_ADMISSION], True, id="second-occurrence-unadmitted"
            ),
            pytest.param(_REPEATED_TEXT, [_REPEATED_FIRST], True, id="repeated-first-only"),
            pytest.param(_REPEATED_TEXT, [_REPEATED_FIRST, _REPEATED_SECOND], False, id="repeated-both"),
            pytest.param(_OVERLAP_TEXT, [_OVERLAP_BEARER], True, id="bearer-does-not-hide-token"),
            pytest.param(_OVERLAP_TEXT, [_OVERLAP_TOKEN], True, id="token-does-not-hide-bearer"),
            pytest.param(_OVERLAP_TEXT, [_OVERLAP_BEARER, _OVERLAP_TOKEN], False, id="both-overlapping-patterns"),
            pytest.param(_KEY_TEXT, [_KEY_ADMISSION], False, id="private-key-marker-admitted"),
            pytest.param(_KEY_MARKER + "\ndifferent body\n", [_KEY_ADMISSION], True, id="private-key-body-changed"),
            pytest.param(_JSON_TEXT, [_JSON_ADMISSION], False, id="json-punctuation-and-crlf"),
            pytest.param(_JSON_TEXT.replace("\r\n", "\n"), [_JSON_ADMISSION], True, id="crlf-normalized"),
            pytest.param(_JSON_TEXT + " ", [_JSON_ADMISSION], True, id="trailing-space"),
            pytest.param(
                _JSON_TEXT,
                [_fixture_admission(_JSON_TEXT, len(_JSON_PREFIX), len(_JSON_TEXT) - 2)],
                True,
                id="wrong-span",
            ),
            pytest.param(
                _JSON_TEXT,
                [_fixture_admission("unrelated", 0, 1), _JSON_ADMISSION],
                False,
                id="unrelated-extra-admission",
            ),
        ],
    )
    def test_exempts_only_exact_admitted_occurrence(
        self, text: str, admissions: list[dict[str, object]], sensitive: bool
    ) -> None:
        """An approved match cannot transfer to changed context, another occurrence, or a changed key body.

        Admissions bind the SHA-256 of the whole UTF-8 input plus a character span and its digest. Cases cover edited or
        extended input, a second occurrence, overlapping bearer and token patterns, a private-key marker whose body
        changed, and JSON punctuation with preserved CRLF where spans count characters but hashes cover bytes.
        """
        result = _load_validator().review_input_has_sensitive_material(text, admissions)

        assert result is sensitive

    @pytest.mark.parametrize(
        "admissions",
        [
            pytest.param(None, id="null"),
            pytest.param({}, id="not-list"),
            pytest.param([{}], id="missing-fields"),
            pytest.param([{**_fixture_admission("abc", 0, 1), "extra": "x"}], id="extra-field"),
            pytest.param([{**_fixture_admission("abc", 0, 1), "input_sha256": "A" * 64}], id="uppercase-input-hash"),
            pytest.param([{**_fixture_admission("abc", 0, 1), "match_sha256": "a" * 63}], id="short-match-hash"),
            pytest.param([{**_fixture_admission("abc", 0, 1), "match_start": True}], id="boolean-offset"),
            pytest.param([{**_fixture_admission("abc", 0, 1), "match_start": -1}], id="negative-offset"),
            pytest.param([{**_fixture_admission("abc", 0, 1), "match_end": 0}], id="empty-span"),
            pytest.param([{**_fixture_admission("abc", 0, 1), "approval_evidence": "  "}], id="blank-approval"),
            pytest.param([{**_fixture_admission("abc", 0, 1), "rationale": None}], id="invalid-rationale"),
            pytest.param([_fixture_admission("abc", 0, 1)] * 2, id="duplicate-binding"),
        ],
    )
    def test_rejects_malformed_admissions(self, admissions: object) -> None:
        """Malformed approval records are rejected even when the inspected context has no secret pattern.

        Each case breaks the admission schema: wrong container, missing or extra fields, bad hashes or offsets, blank
        explanations, or a duplicated binding.
        """
        with pytest.raises(ValueError, match=r"^review-inert-fixture-admissions-invalid$"):
            _load_validator().review_input_has_sensitive_material("ordinary context", admissions)

    @pytest.mark.parametrize("field", ["approval_evidence", "rationale"])
    def test_rejects_sensitive_approval_explanations(self, field: str) -> None:
        """Sensitive text inside the evidence explaining an admission is never exempt.

        The approval evidence or rationale repeats the secret it claims to approve.
        """
        admission = _fixture_admission(_BEARER_MATCH, 0, len(_BEARER_MATCH))
        admission[field] = _BEARER_MATCH

        with pytest.raises(ValueError, match=r"^review-inert-fixture-evidence-sensitive$"):
            _load_validator().review_input_has_sensitive_material(_BEARER_MATCH, [admission])

    def test_inspection_context_admission_preserves_frozen_hash(self, inspection: tuple[dict[str, Any], Path]) -> None:
        """Native review preserves CRLF bytes of a frozen context with an admitted private-key marker.

        The context file is hashed as written, so the validator returns it unchanged for the reviewer role.
        """
        plan, context = inspection

        assert _load_validator().validate_inspection_contexts(plan, context.parent / "plan.json") == {
            "reviewer": context
        }

    def test_inspection_context_rejects_tampering_after_freeze(self, inspection: tuple[dict[str, Any], Path]) -> None:
        """A context changed after it was frozen is rejected.

        Appending text to the context file invalidates both its recorded digest and the admission bound to it.
        """
        plan, context = inspection
        context.write_bytes(context.read_bytes() + b"changed")

        with pytest.raises(ValueError, match=r"^review-inspection-context-hash-mismatch:reviewer$"):
            _load_validator().validate_inspection_contexts(plan, context.parent / "plan.json")


class TestFixtureDiscovery:
    """Fixture discovery lists matches without leaking hashes and exports bindings only for verified input."""

    def test_lists_matches_without_unknown_hashes(self) -> None:
        """Discovery reports spans per input and nothing about values the parent has not verified.

        Two inputs, one holding a bearer header and an embedded token, the other an unrecognised bearer value; only
        pattern indexes and character spans are returned.
        """
        discovery = _load_validator().review_fixture_discovery(_PAGED_INPUTS)

        assert discovery == {
            "schema_version": 1,
            "mode": "fixture-discovery",
            "inputs": [
                {
                    "input_id": "paged:challenger",
                    "matches": [
                        {"pattern_index": 1, "match_start": 3, "match_end": len(_PAGED_TEXT)},
                        {
                            "pattern_index": 3,
                            "match_start": len(_PAGED_TEXT) - len(_PAGED_TOKEN),
                            "match_end": len(_PAGED_TEXT),
                        },
                    ],
                },
                {
                    "input_id": "paged:qa-specialist",
                    "matches": [{"pattern_index": 1, "match_start": 0, "match_end": len(_PAGED_UNKNOWN)}],
                },
            ],
        }

    def test_exports_only_bindings_for_verified_selection(self, verified_selections: dict[str, Any]) -> None:
        """A verified selection of every overlapping match exports binding proposals and nothing else.

        The proposals carry the input digest and each match span and digest, but no hash of the unverified input and no
        approval evidence or rationale text.
        """
        proposals = _load_validator().review_fixture_discovery(_PAGED_INPUTS, verified_selections)

        assert proposals == {
            "schema_version": 1,
            "mode": "fixture-bindings",
            "inputs": [
                {
                    "input_id": "paged:challenger",
                    "bindings": [
                        {
                            "input_sha256": hashlib.sha256(_PAGED_TEXT.encode()).hexdigest(),
                            "match_start": match["match_start"],
                            "match_end": match["match_end"],
                            "match_sha256": match["match_sha256"],
                        }
                        for match in verified_selections["inputs"][0]["matches"]
                    ],
                }
            ],
        }
        exported = json.dumps(proposals)
        assert hashlib.sha256(_PAGED_UNKNOWN.encode()).hexdigest() not in exported
        assert "approval_evidence" not in exported
        assert "rationale" not in exported

    @pytest.mark.parametrize(
        ("inputs", "kept_matches"),
        [
            pytest.param({"paged:challenger": _PAGED_TEXT}, slice(None, -1), id="partial-selection"),
            pytest.param(
                {"paged:challenger": _PAGED_TEXT.replace("e" * 24, "f" * 24)}, slice(None), id="changed-input-value"
            ),
            pytest.param(
                {"paged:challenger": _PAGED_TEXT + "\n" + _PAGED_UNKNOWN}, slice(None), id="extra-unselected-match"
            ),
        ],
    )
    def test_rejects_selection_that_does_not_cover_complete_input(
        self, verified_selections: dict[str, Any], inputs: dict[str, str], kept_matches: slice
    ) -> None:
        """Bindings are exported only after every overlapping match in a selected input is verified.

        The selection omits one match, no longer matches the current input value, or leaves a new match unselected.
        """
        selected = verified_selections["inputs"][0]
        selected["matches"] = selected["matches"][kept_matches]

        with pytest.raises(ValueError, match=r"^review-fixture-selection-unverified$"):
            _load_validator().review_fixture_discovery(inputs, verified_selections)

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda selected: [], id="not-object"),
            pytest.param(_assigning((("schema_version",), True)), id="schema"),
            pytest.param(_assigning((("inputs",), [])), id="empty"),
            pytest.param(_assigning((("inputs", 0, "input_id"), "missing-input")), id="unknown-input"),
            pytest.param(
                lambda selected: _set_path(selected, ("inputs",), selected["inputs"] * 2), id="duplicate-input"
            ),
            pytest.param(
                lambda selected: _set_path(selected, ("inputs", 0, "matches"), selected["inputs"][0]["matches"] * 2),
                id="duplicate-match",
            ),
            pytest.param(_assigning((("inputs", 0, "matches", 0, "match_start"), False)), id="boolean-span"),
            pytest.param(_assigning((("inputs", 0, "matches", 0, "match_sha256"), "A" * 64)), id="hash"),
            pytest.param(
                _assigning(
                    (("inputs", 0, "matches", 0, "approval_evidence"), "Cannot grant approval through discovery.")
                ),
                id="extra-field",
            ),
            pytest.param(_assigning((("inputs", 0, "complete_input_verified"), False)), id="unverified-input"),
            pytest.param(_assigning((("inputs", 0, "verification_evidence"), " ")), id="blank-evidence"),
            pytest.param(_assigning((("inputs", 0, "verification_evidence"), _DIRECT_TEXT)), id="sensitive-evidence"),
        ],
    )
    def test_rejects_invalid_verified_selections(
        self, direct_selection: dict[str, Any], mutate: Callable[[Any], Any]
    ) -> None:
        """Malformed selectors never produce source or credential fingerprints.

        A valid selection of one bearer header is damaged in structure, schema version, duplication, span types, hashes,
        verification flags, or evidence text.
        """
        selected = mutate(direct_selection)

        with pytest.raises(ValueError, match=r"^review-fixture-selection-invalid$"):
            _load_validator().review_fixture_discovery({"direct-plan": _DIRECT_TEXT}, selected)

    def test_private_key_header_requires_complete_input_attestation(self) -> None:
        """Knowing a public header's digest does not attest to the unscanned key body.

        The selection binds only the header digest and omits the complete-input verification, so it is invalid.
        """
        validator = _load_validator()
        header = "-----BEGIN " + "PRIVATE KEY-----"
        text = header + "\nsynthetic body requiring parent inspection\n"
        match = validator.review_fixture_discovery({"direct-plan": text})["inputs"][0]["matches"][0]
        selections = {
            "schema_version": 1,
            "inputs": [
                {
                    "input_id": "direct-plan",
                    "matches": [{**match, "match_sha256": hashlib.sha256(header.encode()).hexdigest()}],
                }
            ],
        }

        assert match["match_end"] == len(header)
        with pytest.raises(ValueError, match=r"^review-fixture-selection-invalid$"):
            validator.review_fixture_discovery({"direct-plan": text}, selections)
