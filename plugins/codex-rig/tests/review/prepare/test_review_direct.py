"""Exercise native direct review without prescribed source transport or scheduling."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from _artifact_files import read_json, read_jsonl
from test_review_prepare import HELPER, _review_inputs, _write_jsonl

OPAQUE_MESSAGE = "gAAAA-retained-host-opaque-message"
OTHER_CHILD_MESSAGE = "gAAAA-other-child-message"
RETRY_REASON = "Original turn aborted; retry after the interruption cleared."


def _direct_prepare(run: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the current default independently from historical paged test helpers."""
    receipt = read_json(run / "local-source/review-worktree.json")
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare",
            "--out",
            str(run),
            "--run-id",
            "direct-test",
            "--parent-thread-id",
            "parent",
            "--source-root",
            receipt["review_worktree"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _direct_evidence(
    tmp_path: Path, *, source_name: str = "widget.py", quoted_coverage: bool = False
) -> tuple[Path, Path]:
    """Prepare a real direct review and retain representative native lifecycle records for each dispatched role.

    The prepare CLI and the isolated checkout are real; only the child's source read, which is fixture content rather
    than behaviour under test, reads the checkout in-process instead of spawning an interpreter per role.
    """
    run = _review_inputs(tmp_path, source_name=source_name)
    prepared = _direct_prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    dispatch = read_json(run / "dispatch.json")
    plan = read_json(run / "direct-plan.json")
    home = tmp_path / "codex-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    parent_model = "test-parent-model"
    parent = [
        {"type": "session_meta", "payload": {"id": "parent", "agent_path": "/root"}},
        {"type": "turn_context", "payload": {"turn_id": "parent-turn", "model": parent_model}},
    ]
    for index, call in enumerate(dispatch["calls"]):
        arguments = call["arguments"]
        agent_path = f"/root/{arguments['task_name']}"
        role = call["role"]
        context = next(item for item in plan["contexts"] if item["role_id"] == role)
        root = Path(plan["source_root"])
        source_read = (root / source_name).read_text(encoding="utf-8") + "\n"
        assert "value = 2" in source_read
        final = (
            "No finding.\n\n## Source Coverage\n"
            + "\n".join(
                f"Reviewed: {json.dumps(path) if quoted_coverage else path}" for path in context["source_paths"]
            )
            + "\n\n## Reviewer Assessment\nSource rating version: 1\nRating: 1\nVerification readiness: complete\nRationale: Inspected assigned source and comparison."
        )
        call_id = f"spawn-{index}"
        parent.extend(
            [
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T10:00:00.000Z",
                    "payload": {
                        "type": "function_call",
                        "name": "collaboration.spawn_agent",
                        "call_id": call_id,
                        "arguments": json.dumps(arguments),
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T10:00:00.100Z",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"task_name": agent_path}),
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T10:00:20.000Z",
                    "payload": {
                        "type": "agent_message",
                        "author": agent_path,
                        "recipient": "/root",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Message Type: FINAL_ANSWER\nTask name: /root\nSender: {agent_path}\nPayload:\n{final}",
                            }
                        ],
                    },
                },
            ]
        )
        _write_jsonl(
            sessions / f"rollout-child-{index}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": f"child-{index}",
                        "timestamp": "2026-01-01T10:00:00.050Z",
                        "parent_thread_id": "parent",
                        "agent_path": agent_path,
                        "agent_role": "default",
                        "source": {
                            "subagent": {"thread_spawn": {"parent_thread_id": "parent", "agent_path": agent_path}}
                        },
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {
                        "turn_id": f"turn-{index}",
                        "model": arguments.get("model", parent_model),
                        "effort": arguments["reasoning_effort"],
                        "sandbox_policy": {"type": "workspace-write"},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "read assigned source", "workdir": str(root)}),
                    },
                },
                {"type": "response_item", "payload": {"type": "function_call_output", "output": source_read}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": f"turn-{index}",
                        "started_at": 1767261601 + index,
                        "completed_at": 1767261610 + index,
                        "last_agent_message": final,
                    },
                },
            ],
        )
    _write_jsonl(sessions / "rollout-parent.jsonl", parent)
    (run / "specialist-assessments.json").write_text(
        json.dumps({call["role"]: {"confidence": 0.95, "blocking_findings": 0} for call in dispatch["calls"]}),
        encoding="utf-8",
    )
    return run, home


def _assemble_direct(run: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Ask the installed-path CLI to bind ordinary native child evidence."""
    return subprocess.run(
        [sys.executable, str(HELPER), "assemble", "--out", str(run), "--codex-home", str(home)],
        capture_output=True,
        text=True,
        check=False,
    )


def _retry_direct(run: Path, home: Path, role: str) -> subprocess.CompletedProcess[str]:
    """Request the bounded replacement of a recorded child failure."""
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare-direct-retry",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            "--role",
            role,
            "--reason",
            RETRY_REASON,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _complete_direct_retry(home: Path, child: list[dict], retry: dict, final: str) -> None:
    """Record the same native replacement lifecycle for aborted and source-incomplete tasks."""
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = read_jsonl(parent_path)
    arguments = retry["arguments"]
    new_path = f"/root/{arguments['task_name']}"
    new_child = json.loads(json.dumps(child))
    new_child[0]["payload"].update(id="retry-child", timestamp="2026-01-01T10:01:00.050Z", agent_path=new_path)
    new_child[0]["payload"]["source"]["subagent"]["thread_spawn"]["agent_path"] = new_path
    new_child[1]["payload"]["turn_id"] = "retry-turn"
    new_child[-1]["payload"] = {
        "type": "task_complete",
        "turn_id": "retry-turn",
        "started_at": 1767261661,
        "completed_at": 1767261670,
        "last_agent_message": final,
    }
    _write_jsonl(home / "sessions/rollout-retry-child.jsonl", new_child)
    parent.extend(
        [
            {
                "type": "response_item",
                "timestamp": "2026-01-01T10:01:00.000Z",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "retry-spawn",
                    "arguments": json.dumps(arguments),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-01-01T10:01:00.100Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "retry-spawn",
                    "output": json.dumps({"task_name": new_path}),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-01-01T10:01:20.000Z",
                "payload": {
                    "type": "agent_message",
                    "author": new_path,
                    "recipient": "/root",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Message Type: FINAL_ANSWER\nTask name: /root\nSender: {new_path}\nPayload:\n{final}",
                        }
                    ],
                },
            },
        ]
    )
    _write_jsonl(parent_path, parent)


def _edit_rollouts(
    home: Path, mutate: Callable[[list[dict[str, object]], list[dict[str, object]]], None]
) -> list[dict[str, object]]:
    """Mutate the parent and first-child rollouts, keep the parent's join text equal to the child's final, save both.

    Returns the edited child rows. Mutations that change the child's final answer therefore reach the join message the
    parent recorded, exactly as a real native lifecycle would report it.
    """
    parent_path = home / "sessions/rollout-parent.jsonl"
    child_path = home / "sessions/rollout-child-0.jsonl"
    parent, child = read_jsonl(parent_path), read_jsonl(child_path)
    mutate(parent, child)
    final = child[-1]["payload"]["last_agent_message"]
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("author") == child[0]["payload"]["agent_path"]:
            text = payload["content"][0]["text"]
            payload["content"][0]["text"] = text.split("Payload:\n", 1)[0] + "Payload:\n" + final
    _write_jsonl(parent_path, parent)
    _write_jsonl(child_path, child)
    return child


def _rewrite_final(home: Path, rewrite: Callable[[str], str]) -> str:
    """Rewrite the first child's final answer (and the parent's join of it) and return the new text."""

    def mutate(parent: list[dict[str, object]], child: list[dict[str, object]]) -> None:
        payload = child[-1]["payload"]
        payload["last_agent_message"] = rewrite(payload["last_agent_message"])

    return _edit_rollouts(home, mutate)[-1]["payload"]["last_agent_message"]


def _child_final(home: Path) -> str:
    """Return the first child's recorded final answer."""
    return read_jsonl(home / "sessions/rollout-child-0.jsonl")[-1]["payload"]["last_agent_message"]


def _first_role(run: Path) -> str:
    """Return the role of the first dispatched direct-review task."""
    return read_json(run / "dispatch.json")["calls"][0]["role"]


def _change_source(run: Path) -> None:
    """Edit the reviewed checkout after the child results were produced."""
    plan = read_json(run / "direct-plan.json")
    (Path(plan["source_root"]) / "widget.py").write_text("value = 999\n", encoding="utf-8")


def _drop_join_messages(parent: list[dict[str, object]], child: list[dict[str, object]]) -> None:
    """Remove every child join message from the parent rollout."""
    parent[:] = [row for row in parent if row.get("payload", {}).get("type") != "agent_message"]


def _misaddress_join_messages(parent: list[dict[str, object]], child: list[dict[str, object]]) -> None:
    """Send every child join message to a recipient other than the parent."""
    for row in parent:
        if row.get("payload", {}).get("type") == "agent_message":
            row["payload"]["recipient"] = "/root/wrong"


def _replace_in_final(old: str, new: str) -> Callable[[list[dict[str, object]], list[dict[str, object]]], None]:
    """Build a mutation replacing text inside the first child's final answer."""

    def mutate(parent: list[dict[str, object]], child: list[dict[str, object]]) -> None:
        payload = child[-1]["payload"]
        payload["last_agent_message"] = payload["last_agent_message"].replace(old, new)

    return mutate


def _deliver_opaque(home: Path, message: str, first_token: str | None, followup_token: str | None) -> None:
    """Replace the dispatched plaintext with ``message`` and model the child's encrypted deliveries.

    ``first_token`` is the encrypted content the child receives first (none means no encrypted delivery);
    ``followup_token`` is a later encrypted delivery inserted before the final turn event.
    """

    def mutate(parent: list[dict[str, object]], child: list[dict[str, object]]) -> None:
        call = next(row["payload"] for row in parent if row.get("payload", {}).get("call_id") == "spawn-0")
        arguments = json.loads(call["arguments"])
        arguments["message"] = message
        call["arguments"] = json.dumps(arguments)
        if first_token is not None:
            delivery = {
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": child[0]["payload"]["agent_path"],
                    "content": [{"type": "encrypted_content", "encrypted_content": first_token}],
                },
            }
            child.insert(2, delivery)
        if followup_token is not None:
            followup = json.loads(json.dumps(child[2]))
            followup["payload"]["content"][0]["encrypted_content"] = followup_token
            child.insert(-1, followup)

    _edit_rollouts(home, mutate)


def _partial_direct_evidence(
    tmp_path: Path, *, source_name: str = "widget.py", coverage_path: str | None = None
) -> tuple[Path, Path, list[dict], str]:
    """Retain an authentic partial first task alongside completed unaffected roles."""
    run, home = _direct_evidence(tmp_path, source_name=source_name)
    original = _child_final(home)
    missing_path = coverage_path if coverage_path is not None else source_name
    child = _edit_rollouts(
        home,
        _replace_in_final(f"Reviewed: {source_name}", f"Missing: {missing_path} — source inspection was interrupted"),
    )
    child = _edit_rollouts(
        home, _replace_in_final("Verification readiness: complete", "Verification readiness: blocked")
    )
    assembled = _assemble_direct(run, home)
    assert assembled.returncode == 0, assembled.stderr
    return run, home, child, original


def _supports_control_names() -> bool:
    """Probe whether this filesystem can create the control-character fixture."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "a\nb\tc").write_bytes(b"x")
        except OSError:
            return False
    return True


_skip_without_control_names = pytest.mark.skipif(
    not _supports_control_names(), reason="filesystem rejects control-character names"
)


@pytest.fixture
def evidence(tmp_path: Path) -> tuple[Path, Path]:
    """Prepare a direct review (run directory, Codex home) with ordinary completed native child evidence."""
    return _direct_evidence(tmp_path)


@pytest.fixture
def partial_run(tmp_path: Path) -> SimpleNamespace:
    """Direct review whose first role reported an honest source gap and was assembled for recovery."""
    run, home, child, original = _partial_direct_evidence(tmp_path)
    return SimpleNamespace(run=run, home=home, child=child, final=original, role=_first_role(run))


@pytest.fixture
def aborted_run(evidence: tuple[Path, Path]) -> SimpleNamespace:
    """Direct review whose first child turn was aborted: its assembly is rejected and no manifest exists."""
    run, home = evidence
    original = home / "sessions/rollout-child-0.jsonl"
    child = read_jsonl(original)
    original_path = child[0]["payload"]["agent_path"]
    final = child[-1]["payload"]["last_agent_message"]
    child[-1]["payload"] = {"type": "turn_aborted", "turn_id": "turn-0"}
    _write_jsonl(original, child)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [row for row in read_jsonl(parent_path) if row.get("payload", {}).get("author") != original_path]
    _write_jsonl(parent_path, parent)
    failed_assembly = _assemble_direct(run, home)
    assert failed_assembly.returncode != 0
    assert not (run / "specialist-manifest.json").exists()
    return SimpleNamespace(run=run, home=home, child=child, final=final, role=_first_role(run))


@pytest.mark.integration
class TestDirectPrepare:
    """Protect how the prepare command turns a routed review into direct, pointer-style tasks."""

    def test_default_review_prepares_one_pointer_task_per_role(self, tmp_path: Path) -> None:
        """Large source must not multiply a routed role into paged serial waves.

        A large-source review is prepared through the real CLI; each triggered role gets exactly one task whose message
        points at the retained role card and patch instead of carrying paged context-reader instructions.
        """
        run = _review_inputs(tmp_path, large_source=True)

        result = _direct_prepare(run)

        assert result.returncode == 0, result.stderr
        dispatch = read_json(run / "dispatch.json")
        routing = read_json(run / "review-routing.json")
        messages = [call["arguments"]["message"] for call in dispatch["calls"]]
        malformed = [
            message
            for message in messages
            if not ("Reviewed:" in message and "role-cards" in message and "diff.patch" in message)
            or "unchanged =" in message
            or "review_context.py" in message
        ]
        assert dispatch["mode"] == "direct-review"
        assert {call["role"] for call in dispatch["calls"]} == set(routing["triggered_roles"])
        assert len(dispatch["calls"]) == len(routing["triggered_roles"])
        assert "waves" not in dispatch
        assert not (run / "inspection-plan.json").exists()
        assert not malformed


@pytest.mark.integration
class TestDirectAssembly:
    """Protect assembly of ordinary native child evidence into an accepted, replay-safe specialist manifest."""

    def test_renamed_dispatch_reports_missing_expected_task(self, evidence: tuple[Path, Path]) -> None:
        """Reject consistently renamed native evidence without misdiagnosing duplicate children."""
        run, home = evidence
        call = read_json(run / "dispatch.json")["calls"][0]
        task = call["arguments"]["task_name"]
        parent = home / "sessions/rollout-parent.jsonl"
        child = home / "sessions/rollout-child-0.jsonl"
        parent.write_text(parent.read_text(encoding="utf-8").replace(task, "renamed_review"), encoding="utf-8")
        child.write_text(child.read_text(encoding="utf-8").replace(task, "renamed_review"), encoding="utf-8")

        result = _assemble_direct(run, home)

        assert result.returncode != 0
        assert f"review-child-session-missing:{call['role']}:{task}" in result.stderr
        assert "session-not-unique" not in result.stderr
        assert not (run / "specialist-manifest.json").exists()

    def test_duplicate_expected_child_remains_rejected(self, evidence: tuple[Path, Path]) -> None:
        """Retain the ambiguity guard when two rollout files claim the exact prepared child."""
        run, home = evidence
        role = read_json(run / "dispatch.json")["calls"][0]["role"]
        child = home / "sessions/rollout-child-0.jsonl"
        (home / "sessions/duplicate-child.jsonl").write_bytes(child.read_bytes())

        result = _assemble_direct(run, home)

        assert result.returncode != 0
        assert f"review-child-session-not-unique:{role}" in result.stderr
        assert not (run / "specialist-manifest.json").exists()

    def test_accepts_ordinary_reads_and_is_idempotent(self, evidence: tuple[Path, Path]) -> None:
        """Native children may use ordinary tools while completion remains bound to real joins.

        Assembling the same evidence twice succeeds and yields the current manifest schema, so a retry of the command
        after an interruption cannot corrupt an accepted review.
        """
        run, home = evidence

        first = _assemble_direct(run, home)

        assert first.returncode == 0, first.stderr
        summary = json.loads(first.stdout)
        assert summary["source_consumption"] == "reviewer-reported"
        assert summary["actual_mode"] == "parallel"
        assert all(value["sandbox_mode"] == "workspace-write" for value in summary["observed_controls"].values())
        assert _assemble_direct(run, home).returncode == 0
        assert read_json(run / "specialist-manifest.json")["schema_version"] == 10

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_drop_join_messages, id="join"),
            pytest.param(_misaddress_join_messages, id="recipient"),
            pytest.param(_replace_in_final("Reviewed:", "Skipped:"), id="coverage"),
            pytest.param(
                _replace_in_final("Reviewed: widget.py", "Reviewed: widget.py unassigned \t"), id="path-suffix"
            ),
            pytest.param(_replace_in_final("Verification readiness: complete\n", ""), id="assessment"),
            pytest.param(
                lambda parent, child: child[0]["payload"]["source"]["subagent"]["thread_spawn"].update(
                    parent_thread_id="other"
                ),
                id="parent",
            ),
            pytest.param(lambda parent, child: child[1]["payload"].update(model="unexpected-model"), id="model"),
        ],
    )
    def test_rejects_unverified_evidence(
        self, evidence: tuple[Path, Path], damage: Callable[[list[dict], list[dict]], None]
    ) -> None:
        """Independent lifecycle or source defects cannot become accepted specialist coverage.

        Each case breaks one link of the child evidence (join, recipient, coverage claim, assigned-path match,
        assessment fields, parentage, or model) and assembly must fail without writing a manifest.
        """
        run, home = evidence
        _edit_rollouts(home, damage)

        result = _assemble_direct(run, home)

        assert result.returncode != 0
        assert not (run / "specialist-manifest.json").exists()

    def test_rejects_changed_source(self, evidence: tuple[Path, Path]) -> None:
        """A checkout edited after the children read it cannot back accepted specialist coverage."""
        run, home = evidence
        _change_source(run)

        result = _assemble_direct(run, home)

        assert result.returncode != 0
        assert not (run / "specialist-manifest.json").exists()

    def test_accepts_consistent_repeated_parent_context(self, evidence: tuple[Path, Path]) -> None:
        """Compaction may repeat a turn context when its inherited model stays consistent."""
        run, home = evidence
        parent_path = home / "sessions/rollout-parent.jsonl"
        repeated = {"type": "turn_context", "payload": {"turn_id": "parent-turn", "model": "test-parent-model"}}
        _write_jsonl(parent_path, [*read_jsonl(parent_path), repeated])

        result = _assemble_direct(run, home)

        assert result.returncode == 0, result.stderr
        assert read_json(run / "specialist-manifest.json")["schema_version"] == 10

    def test_rejects_conflicting_repeated_parent_context(self, evidence: tuple[Path, Path]) -> None:
        """A repeated parent turn context naming a different model is ambiguous and is rejected."""
        run, home = evidence
        parent_path = home / "sessions/rollout-parent.jsonl"
        repeated = {"type": "turn_context", "payload": {"turn_id": "parent-turn", "model": "conflicting-model"}}
        _write_jsonl(parent_path, [*read_jsonl(parent_path), repeated])

        result = _assemble_direct(run, home)

        assert result.returncode != 0
        assert "review-parent-model-context-ambiguous" in result.stderr
        assert not (run / "specialist-manifest.json").exists()

    def test_keeps_selected_review_after_completed_followup(self, evidence: tuple[Path, Path]) -> None:
        """A later completed reminder turn cannot replace or invalidate the original accepted review.

        After a first accepted assembly, the child records an extra acknowledged reminder turn; reassembly must leave
        the manifest byte-identical.
        """
        run, home = evidence
        initial = _assemble_direct(run, home)
        assert initial.returncode == 0, initial.stderr
        manifest = (run / "specialist-manifest.json").read_bytes()
        child_path = home / "sessions/rollout-child-0.jsonl"
        child = read_jsonl(child_path)
        later_context = json.loads(json.dumps(child[1]))
        later_context["payload"]["turn_id"] = "later-progress-turn"
        completion = {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "later-progress-turn",
                "started_at": 1767261661,
                "completed_at": 1767261670,
                "last_agent_message": "Acknowledged reminder.",
            },
        }
        _write_jsonl(child_path, [*child, later_context, completion])

        result = _assemble_direct(run, home)

        assert result.returncode == 0, result.stderr
        assert (run / "specialist-manifest.json").read_bytes() == manifest

    def test_promotion_rechecks_source_after_assembly(self, evidence: tuple[Path, Path]) -> None:
        """A valid manifest cannot approve a checkout changed after child results were assembled.

        Assembly succeeds first; only then is the checkout edited, and the shipped manifest validator must notice.
        """
        run, home = evidence
        assembled = _assemble_direct(run, home)
        assert assembled.returncode == 0, assembled.stderr
        _change_source(run)

        result = subprocess.run(
            [
                sys.executable,
                str(HELPER.with_name("validate_artifacts.py")),
                "--out",
                str(run),
                "--manifest-only",
                "--codex-home",
                str(home),
                "--parent-thread-id",
                "parent",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "review-direct-source-invalid" in result.stderr


@pytest.mark.integration
class TestDirectDelivery:
    """Protect how an encrypted host delivery binds a child to its parent's dispatched task."""

    @pytest.mark.parametrize(
        ("first_token", "followup_token"),
        [
            pytest.param(OPAQUE_MESSAGE, None, id="opaque"),
            pytest.param(OPAQUE_MESSAGE, "gAAAA-later-progress-reminder", id="followup"),
        ],
    )
    def test_accepts_matching_opaque_delivery(
        self, evidence: tuple[Path, Path], first_token: str, followup_token: str | None
    ) -> None:
        """An encrypted delivery equal to the parent's dispatched message binds, even with a later reminder."""
        run, home = evidence
        _deliver_opaque(home, OPAQUE_MESSAGE, first_token, followup_token)

        result = _assemble_direct(run, home)

        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        ("message", "first_token", "followup_token"),
        [
            pytest.param(OPAQUE_MESSAGE, OTHER_CHILD_MESSAGE, OPAQUE_MESSAGE, id="wrong-first-token"),
            pytest.param(OPAQUE_MESSAGE, OPAQUE_MESSAGE, OPAQUE_MESSAGE, id="replayed-token"),
            pytest.param(OPAQUE_MESSAGE, OTHER_CHILD_MESSAGE, None, id="mismatched-token"),
            pytest.param("Changed task", None, None, id="altered-plaintext"),
        ],
    )
    def test_rejects_unbound_delivery(
        self, evidence: tuple[Path, Path], message: str, first_token: str | None, followup_token: str | None
    ) -> None:
        """A delivery for another message, a replayed token, or altered plaintext never binds parent and child.

        The first encrypted delivery must match the dispatched message; a later copy of it is a replay, and plaintext
        dispatch must stay exact.
        """
        run, home = evidence
        _deliver_opaque(home, message, first_token, followup_token)

        result = _assemble_direct(run, home)

        assert result.returncode != 0
        assert not (run / "specialist-manifest.json").exists()


@pytest.mark.integration
class TestDirectCoverage:
    """Protect how reviewer-reported source coverage must match the assigned paths exactly."""

    @pytest.mark.parametrize(
        "coverage",
        [
            "## Instructions copied for reference\n```text\nReviewed: widget.py\n```",
            "## Source Coverage\n```text\nReviewed: widget.py\n```",
            "## Source Coverage\nReviewed: widget.py\n## Source Coverage\nReviewed: widget.py",
            "## Source Coverage\nReviewed: widget.py\nMissing: widget.py — unavailable",
            "## Source Coverage\nReviewed: widget.py\n- Reviewed: `widget.py`",
            "## Source Coverage\nReviewed: `other.py`",
            "## Source Coverage\nMissing: widget.py",
            "## Source Coverage\nMissing: widget.py —   ",
        ],
    )
    @pytest.mark.parametrize("source_name", ["widget.py", "alpha — beta.py"])
    def test_requires_unique_unfenced_section_claims(self, tmp_path: Path, coverage: str, source_name: str) -> None:
        """Examples and contradictory declarations cannot stand in for an actual coverage report.

        Fenced examples, a missing section heading, repeated sections, mixed reviewed/missing claims, and the wrong path
        are all rejected, for an ordinary name and for one containing the em dash used as a reason delimiter.
        """
        run, home = _direct_evidence(tmp_path, source_name=source_name)
        _rewrite_final(
            home,
            lambda final: final.replace(
                f"## Source Coverage\nReviewed: {source_name}", coverage.replace("widget.py", source_name)
            ),
        )

        result = _assemble_direct(run, home)

        assert result.returncode != 0
        assert "coverage" in result.stderr or "review-direct-missing-reason-required:" in result.stderr
        assert not (run / "specialist-manifest.json").exists()

    @pytest.mark.parametrize(
        "declaration",
        [
            pytest.param("Reviewed: `widget.py`", id="inline-code"),
            pytest.param("- Reviewed: widget.py", id="dash-bullet"),
            pytest.param("* Reviewed: `widget.py`", id="star-bullet"),
            pytest.param("+ Reviewed: `widget.py`", id="plus-bullet"),
            pytest.param("Reviewed: widget.py  ", id="hard-break"),
            pytest.param("Reviewed: `widget.py`  ", id="quoted-hard-break"),
            pytest.param("- Reviewed: `widget.py`\t", id="tab-padding"),
            pytest.param("Reviewed:   widget.py ", id="colon-padding"),
        ],
    )
    def test_accepts_equivalent_markdown_paths(self, evidence: tuple[Path, Path], declaration: str) -> None:
        """Markdown padding, including hard breaks in real reviewer output, preserves exact assigned coverage.

        The raw reviewer output is retained byte-for-byte even though the declaration is normalized for matching.
        """
        run, home = evidence
        final = _rewrite_final(home, lambda text: text.replace("Reviewed: widget.py", declaration))

        result = _assemble_direct(run, home)

        assert result.returncode == 0, result.stderr
        manifest = read_json(run / "specialist-manifest.json")
        selected = next(item for item in manifest["passes"] if item["attempts"][0]["agent_thread_id"] == "child-0")
        assert (run / selected["attempts"][0]["raw_output_path"]).read_text(encoding="utf-8") == final

    @pytest.mark.parametrize(
        "source_name",
        [
            pytest.param("`widget`.py", id="literal-backticks"),
            pytest.param(" widget.py", id="leading-space"),
            pytest.param("widget file.py", id="interior-space"),
        ],
    )
    def test_preserves_literal_filenames(self, tmp_path: Path, source_name: str) -> None:
        """Exact assigned filenames win before Markdown whitespace and backtick normalization."""
        run, home = _direct_evidence(tmp_path, source_name=source_name)

        result = _assemble_direct(run, home)

        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout)
        assert all(value["reviewed"] == [source_name] for value in summary["source_coverage"].values())

    @_skip_without_control_names
    def test_accepts_json_quoted_control_character_filename(self, tmp_path: Path) -> None:
        """Retain exact control-character source names through real dispatch and assembly."""
        name = "a\nb\tc.py"
        run, home = _direct_evidence(tmp_path, source_name=name, quoted_coverage=True)

        result = _assemble_direct(run, home)

        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout)
        assert all(value["reviewed"] == [name] for value in summary["source_coverage"].values())

    @pytest.mark.parametrize(
        ("source_name", "coverage_path"),
        [
            pytest.param("widget.py", "widget.py", id="ordinary-path"),
            pytest.param("alpha — beta.py", "alpha — beta.py", id="literal-delimiter"),
            pytest.param("alpha — beta.py", "`alpha — beta.py`", id="inline-code-delimiter"),
            pytest.param("alpha — beta.py", json.dumps("alpha — beta.py"), id="json-delimiter"),
            pytest.param("alpha —", "alpha —", id="overlapping-delimiters"),
        ],
    )
    def test_missing_coverage_preserves_assigned_path_and_allows_retry(
        self, tmp_path: Path, source_name: str, coverage_path: str
    ) -> None:
        """A filename delimiter belongs to the assigned path before the separate missing-source reason.

        A partial report names the unreachable path in several spellings; the recovery summary must attribute the gap to
        the assigned path only, and the affected role must stay eligible for a bounded retry.
        """
        run, home, _, _ = _partial_direct_evidence(tmp_path, source_name=source_name, coverage_path=coverage_path)
        role = _first_role(run)

        assembled = _assemble_direct(run, home)
        retry = _retry_direct(run, home, role)

        assert assembled.returncode == 0, assembled.stderr
        coverage = json.loads(assembled.stdout)["source_coverage"]
        assert coverage[role] == {"reviewed": [], "missing": [f"{source_name} — source inspection was interrupted"]}
        assert all(
            item["reviewed"] == [source_name] and item["missing"] == [] for key, item in coverage.items() if key != role
        )
        assert retry.returncode == 0, retry.stderr
        assert json.loads(retry.stdout)["role"] == role


@pytest.mark.integration
class TestDirectRetry:
    """Protect the bounded replacement of a failed or source-incomplete direct review task."""

    @pytest.mark.parametrize("schema", [2, 3])
    def test_unreadable_session_prevents_no_child_retry(self, evidence: tuple[Path, Path], schema: int) -> None:
        """Unknown child metadata cannot authorize replacing a possibly completed original task."""
        run, home = evidence
        plan_path = run / "direct-plan.json"
        plan = read_json(plan_path)
        plan["schema_version"] = schema
        plan_path.write_bytes(json.dumps(plan).encode("utf-8"))
        role = _first_role(run)
        (home / "sessions/rollout-child-0.jsonl").write_bytes(b"\xff\n")

        result = _retry_direct(run, home, role)

        assert result.returncode != 0
        assert "provenance-session-unreadable" in result.stderr
        assert not (run / f"retry/{role}.json").exists()

    def test_retains_honest_missing_source_for_recovery(self, partial_run: SimpleNamespace) -> None:
        """Recovery keeps completed reviewer evidence while assessed-result admission rejects missing inspection.

        One role reported an interrupted source read; reassembly reports exactly that gap and leaves the retained
        manifest byte-identical.
        """
        run, home = partial_run.run, partial_run.home

        assembled = _assemble_direct(run, home)

        assert assembled.returncode == 0, assembled.stderr
        coverage = json.loads(assembled.stdout)["source_coverage"]
        retained = (run / "specialist-manifest.json").read_bytes()
        assert sum(bool(item["missing"]) for item in coverage.values()) == 1
        assert any(item["missing"] == ["widget.py — source inspection was interrupted"] for item in coverage.values())
        assert _assemble_direct(run, home).returncode == 0
        assert (run / "specialist-manifest.json").read_bytes() == retained

    def test_cannot_replace_completed_review(self, evidence: tuple[Path, Path]) -> None:
        """A clean or inconvenient completed result is never a genuine retry failure."""
        run, home = evidence
        role = _first_role(run)

        result = _retry_direct(run, home, role)

        assert result.returncode != 0
        assert "completed-result-replacement-forbidden" in result.stderr
        assert not (run / f"retry/{role}.json").exists()

    def test_retains_aborted_original_and_reuses_completed_roles(self, aborted_run: SimpleNamespace) -> None:
        """Resume one genuinely aborted role without respawning successful role reviews.

        The first child's turn was aborted, so assembly was rejected; after a bounded retry completes, the replaced role
        selects attempt 2 and keeps its failure record while every other role stays on attempt 1.
        """
        run, home, role = aborted_run.run, aborted_run.home, aborted_run.role

        retry_result = _retry_direct(run, home, role)

        assert retry_result.returncode == 0, retry_result.stderr
        retry = json.loads(retry_result.stdout)
        _complete_direct_retry(home, aborted_run.child, retry, aborted_run.final)
        result = _assemble_direct(run, home)
        assert result.returncode == 0, result.stderr
        manifest = read_json(run / "specialist-manifest.json")
        retried = next(item for item in manifest["passes"] if item["role"] == role)
        assert retried["selected_attempt"] == 2
        assert (run / retry["failure_path"]).exists()
        assert all(item["selected_attempt"] == 1 for item in manifest["passes"] if item["role"] != role)
        assert _assemble_direct(run, home).returncode == 0

    def test_partial_retry_completes_only_missing_role_and_preserves_original(
        self, partial_run: SimpleNamespace
    ) -> None:
        """Complete one authenticated source gap without discarding original findings or other role evidence.

        The retried role moves to attempt 2 with a complete report, the other roles' passes stay untouched, the original
        manifest, summary and raw output are retained, and a second retry is refused.
        """
        run, home, role = partial_run.run, partial_run.home, partial_run.role
        original = (run / "specialist-manifest.json").read_bytes()
        original_summary = (run / "inspection-summary.json").read_bytes()
        original_raw = (run / f"specialists/{role}.raw.md").read_bytes()

        retry_result = _retry_direct(run, home, role)

        assert retry_result.returncode == 0, retry_result.stderr
        retry = json.loads(retry_result.stdout)
        assert "preserve every original finding as a reconciliation obligation" in retry["arguments"]["message"]
        assert (run / f"retry/{role}.manifest.json").read_bytes() == original
        assert (run / f"retry/{role}.summary.json").read_bytes() == original_summary
        _complete_direct_retry(home, partial_run.child, retry, partial_run.final)
        assembled = _assemble_direct(run, home)
        assert assembled.returncode == 0, assembled.stderr
        assert all(not item["missing"] for item in json.loads(assembled.stdout)["source_coverage"].values())
        current = read_json(run / "specialist-manifest.json")
        previous = json.loads(original)
        assert next(item for item in current["passes"] if item["role"] == role)["selected_attempt"] == 2
        assert [item for item in current["passes"] if item["role"] != role] == [
            item for item in previous["passes"] if item["role"] != role
        ]
        assert (run / f"specialists/{role}.raw.md").read_bytes() == original_raw
        assert _assemble_direct(run, home).returncode == 0
        assert "retry-already-completed" in _retry_direct(run, home, role).stderr

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(lambda run, role: _change_source(run), id="source"),
            pytest.param(lambda run, role: (run / "result.json").write_text("{}\n", encoding="utf-8"), id="promoted"),
            pytest.param(
                lambda run, role: (run / f"specialists/{role}.md").write_text(
                    (run / f"specialists/{role}.md").read_text(encoding="utf-8")
                    + "\nMissing: invented.py — unavailable\n",
                    encoding="utf-8",
                ),
                id="forged-coverage",
            ),
        ],
    )
    def test_partial_retry_rejects_changed_or_ineligible_origin(
        self, partial_run: SimpleNamespace, damage: Callable[[Path, str], object]
    ) -> None:
        """Source changes, promoted results, and fabricated gap text cannot authorize replacement."""
        run, home, role = partial_run.run, partial_run.home, partial_run.role
        damage(run, role)

        rejected = _retry_direct(run, home, role)

        assert rejected.returncode != 0
        assert not (run / f"retry/{role}.json").exists()

    def test_partial_retry_does_not_refresh_for_another_partial_result(self, partial_run: SimpleNamespace) -> None:
        """An incomplete replacement retains failure evidence without replacing the original derived artifacts."""
        run, home, role = partial_run.run, partial_run.home, partial_run.role
        original = (run / "specialist-manifest.json").read_bytes()
        original_summary = (run / "inspection-summary.json").read_bytes()
        prepared = _retry_direct(run, home, role)
        assert prepared.returncode == 0, prepared.stderr
        _complete_direct_retry(
            home, partial_run.child, json.loads(prepared.stdout), partial_run.child[-1]["payload"]["last_agent_message"]
        )

        rejected = _assemble_direct(run, home)

        assert rejected.returncode != 0
        assert "partial-retry-source-incomplete" in rejected.stderr
        assert (run / "specialist-manifest.json").read_bytes() == original
        assert (run / "inspection-summary.json").read_bytes() == original_summary


def _write_nested_owner_rollout(home: Path, keep: Callable[[dict[str, object]], bool], *, forkless: bool) -> None:
    """Rewrite the parent rollout as a nested subagent that inherits its ancestor's history, then its own."""
    rollout = home / "sessions/rollout-parent.jsonl"
    rows = read_jsonl(rollout)
    owner = {
        **rows[0]["payload"],
        "multi_agent_version": "v2",
        "thread_source": "subagent",
        "history_mode": "paginated",
        "subagent_history_start_ordinal": len(rows) + 1,
        "parent_thread_id": "ancestor",
        "forked_from_id": "ancestor",
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "ancestor"}}},
    }
    nested = [
        {"type": "session_meta", "payload": owner},
        {"type": "session_meta", "payload": {"id": "ancestor"}},
        *rows[1:],
        *[row for row in rows[1:] if keep(row)],
    ]
    if forkless:
        nested[1]["payload"].update(
            parent_thread_id="execution-parent-not-inherited",
            source={"subagent": {"thread_spawn": {"parent_thread_id": "execution-parent-not-inherited"}}},
        )
    _write_jsonl(rollout, [{**row, "ordinal": index} for index, row in enumerate(nested)])


@pytest.mark.integration
class TestNestedOwnerHistory:
    """Protect binding of native review evidence to the nested owner's own history, not inherited receipts."""

    @pytest.mark.parametrize("forkless", [False, True])
    def test_accepts_complete_owner_evidence(self, evidence: tuple[Path, Path], forkless: bool) -> None:
        """Inherited duplicate receipts cannot invalidate a nested parent's complete native review evidence.

        The owner's own spawn and join rows follow a duplicated inherited copy; assembly still succeeds, with or without
        a fork relation between the owner and the inherited ancestor.
        """
        run, home = evidence
        _write_nested_owner_rollout(home, lambda row: True, forkless=forkless)

        result = _assemble_direct(run, home)

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["actual_mode"] == "parallel"

    @pytest.mark.parametrize("forkless", [False, True])
    @pytest.mark.parametrize(
        "keep",
        [
            pytest.param(
                lambda row: row.get("payload", {}).get("name", "").rsplit(".", 1)[-1] != "spawn_agent",
                id="missing-spawn",
            ),
            pytest.param(lambda row: row.get("payload", {}).get("type") != "agent_message", id="missing-join"),
        ],
    )
    def test_rejects_evidence_supplied_only_by_inherited_history(
        self, evidence: tuple[Path, Path], keep: Callable[[dict[str, object]], bool], forkless: bool
    ) -> None:
        """Inherited duplicate receipts cannot supply a nested owner's missing spawn or join evidence."""
        run, home = evidence
        _write_nested_owner_rollout(home, keep, forkless=forkless)

        result = _assemble_direct(run, home)

        assert result.returncode != 0
        assert "review-spawn-call-not-unique" in result.stderr or "review-direct-parent-join" in result.stderr
