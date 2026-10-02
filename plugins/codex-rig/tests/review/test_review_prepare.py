"""Exercise deterministic review preparation without model-authored provenance."""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import math
import re
import runpy
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills/code-review"
HELPER = SKILL / "review_prepare.py"
_CONTEXT_READER = runpy.run_path(str(SKILL / "review_context.py"))["read_context"]


def _review_inputs(
    tmp_path: Path,
    *,
    untracked: bool = False,
    second_file: bool = False,
    unchanged_caller: bool = False,
    deleted: bool = False,
    large_source: bool = False,
    large_patch: bool = False,
) -> Path:
    """Write semantic reviewer decisions while leaving mechanical evidence to the producer."""
    spec = importlib.util.spec_from_file_location("prepare_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = tmp_path / "review"
    run.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    # Git checks out unchanged callers from HEAD, so writer LF alone cannot fix their checkout bytes.
    for command in (
        ["init", "-q"],
        ["config", "core.autocrlf", "false"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.com"],
    ):
        subprocess.run(["git", "-C", str(repository), *command], check=True, capture_output=True)
    stable_source = "unchanged = 'café'\n" * 18000 if large_source else ""
    (repository / "widget.py").write_text("value = 1\n" + stable_source, encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(repository), "add", "widget.py"], check=True, capture_output=True)
    if second_file:
        (repository / "other.py").write_text("other = 1\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "-C", str(repository), "add", "other.py"], check=True, capture_output=True)
    if unchanged_caller:
        (repository / "stable.py").write_text("caller = 5\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "-C", str(repository), "add", "stable.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "Initial"], check=True, capture_output=True)
    (repository / "widget.py").write_text(
        "value = 2\n" + ("changed = 'café'\n" * 18000 if large_patch else stable_source), encoding="utf-8", newline="\n"
    )
    if second_file:
        (repository / "other.py").write_text("other = 2\n", encoding="utf-8", newline="\n")
    if deleted:
        (repository / "widget.py").unlink()
    if untracked:
        (repository / "new.txt").write_text("new_value = 7\n", encoding="utf-8", newline="\n")
    collected = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/collect_diff.py"),
            "--review-worktree",
            "--repository",
            str(repository),
            "--out",
            str(run / "local-source"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert collected.returncode == 0, collected.stderr
    (run / "diff.patch").write_bytes((run / "local-source/diff.patch").read_bytes())
    if untracked:
        (run / "untracked.txt").write_text("new.txt\n", encoding="utf-8", newline="\n")
    roles = ["challenger", "qa-specialist"]
    routing = {
        "schema_version": 1,
        "risk_tier": "HIGH_RISK",
        "signals": dict.fromkeys(module.ROUTING_SIGNALS, False),
        "signal_evidence": {name: ["Scope checked."] for name in module.ROUTING_SIGNALS},
        "triggered_roles": roles,
        "trigger_reasons": {role: ["High-risk behavior."] for role in roles},
    }
    (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    briefs = {}
    for role in roles:
        (run / f"{role}-evidence.md").write_text(
            f"Scope: widget.py at frozen revision. Excluded: unrelated callers.\nQuestion: {role} axis.\n"
            + "Relevant source evidence.\n" * 300,
            encoding="utf-8",
            newline="\n",
        )
        briefs[role] = {"axis": role, "evidence_path": f"{role}-evidence.md", "source_paths": ["widget.py"]}
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    return run


def test_review_source_fixture_preserves_utf8_lf_under_windows_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep frozen source bytes portable under Windows text and Git checkout defaults."""
    write_text = Path.write_text
    git_config = tmp_path / "gitconfig"
    git_config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def windows_write_text(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        """Apply Windows text defaults unless the fixture explicitly selects its byte format."""
        return write_text(path, data, encoding=encoding or "cp1252", errors=errors, newline=newline or "\r\n")

    monkeypatch.setattr(Path, "write_text", windows_write_text)
    run = _review_inputs(tmp_path, large_source=True, second_file=True, unchanged_caller=True)
    root = Path(json.loads((run / "local-source/review-worktree.json").read_bytes())["review_worktree"])
    source = (root / "widget.py").read_bytes()

    assert source == ("value = 2\n" + "unchanged = 'café'\n" * 18000).encode("utf-8")
    assert (root / "other.py").read_bytes() == b"other = 2\n"
    assert (root / "stable.py").read_bytes() == b"caller = 5\n"


def _prepare(
    run: Path, *, source_root: Path | None = None, expected_head: str | None = None, scope_path: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke the shipped producer as an installed-path command."""
    if source_root is None:
        source_root = Path(
            json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
        )
    command = [
        sys.executable,
        str(HELPER),
        "prepare",
        "--out",
        str(run),
        "--run-id",
        "bounded-review",
        "--parent-thread-id",
        "parent",
        "--source-root",
        str(source_root),
    ]
    if expected_head is not None:
        command.extend(["--expected-head", expected_head])
        base = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", f"{expected_head}^"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        command.extend(["--expected-diff-base", base])
    if scope_path is not None:
        command.extend(["--scope-path", scope_path])
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_prepare_rejects_missing_signals_without_rewriting_routing(tmp_path: Path) -> None:
    """Reject omitted semantic booleans before canonicalization or reviewer preparation."""
    run = _review_inputs(tmp_path)
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    del routing["signals"]
    original = json.dumps(routing).encode("utf-8")
    routing_path.write_bytes(original)

    result = _prepare(run)

    assert result.returncode != 0
    assert "review-routing-signal-set-mismatch" in result.stderr
    assert routing_path.read_bytes() == original
    assert not (run / "inspection-plan.json").exists()
    assert not (run / "specialists").exists()


def test_prepare_rejects_omitted_local_diff(tmp_path: Path) -> None:
    """Reject a complete checkout paired with an incomplete admitted patch."""
    run = _review_inputs(tmp_path, second_file=True)
    root = json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    patch = subprocess.run(
        ["git", "-C", root, "diff", "--binary", "HEAD", "--", "widget.py"], check=True, capture_output=True
    ).stdout
    (run / "diff.patch").write_bytes(patch)
    result = _prepare(run)
    assert result.returncode == 2
    assert "review-source-diff-stale" in result.stderr
    assert not (run / "dispatch.json").exists()


def test_prepare_rejects_missing_untracked_inventory(tmp_path: Path) -> None:
    """Keep retained untracked changes from disappearing before reviewer dispatch."""
    run = _review_inputs(tmp_path, untracked=True)
    (run / "untracked.txt").unlink()
    result = _prepare(run)
    assert result.returncode == 2
    assert "review-source-untracked-stale" in result.stderr
    assert not (run / "dispatch.json").exists()


def test_prepare_accepts_declared_local_path_scope(tmp_path: Path) -> None:
    """Allow intentional path reviews without silently dropping whole-tree evidence."""
    run = _review_inputs(tmp_path, second_file=True, untracked=True)
    root = json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    patch = subprocess.run(
        ["git", "-C", root, "diff", "--binary", "HEAD", "--", "widget.py"], check=True, capture_output=True
    ).stdout
    (run / "diff.patch").write_bytes(patch)
    (run / "untracked.txt").write_bytes(b"")
    result = _prepare(run, scope_path="widget.py")
    assert result.returncode == 0, result.stderr
    assert (run / "dispatch.json").exists()


def test_prepare_rejects_undelivered_changed_source(tmp_path: Path) -> None:
    """A full patch cannot hide a changed file from every reviewer context."""
    run = _review_inputs(tmp_path, second_file=True)
    result = _prepare(run)
    assert result.returncode == 2
    assert "review-source-coverage-incomplete" in result.stderr
    diagnostic = json.loads(result.stderr.split("review-source-coverage-incomplete:", 1)[1])
    assert diagnostic == {"missing_diff_paths": [], "missing_source_paths": ["other.py"]}
    assert not (run / "dispatch.json").exists()


@pytest.mark.parametrize("default_encoding", ["utf-8", "cp1252"])
def test_prepare_accepts_complete_split_source_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default_encoding: str
) -> None:
    """Reviewers may receive disjoint source while collectively covering every change."""
    read_text = Path.read_text

    def read_with_default(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        """Simulate the platform default while respecting explicit artifact encodings."""
        return read_text(path, encoding=encoding or default_encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_with_default)
    run = _review_inputs(tmp_path, second_file=True)
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"] = ["other.py"]
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    for context in plan["contexts"]:
        text = (run / context["context_path"]).read_text(encoding="utf-8")
        assert ("other = 2" in text) is (context["role_id"] == "challenger")
        assert ("value = 2" in text) is (context["role_id"] != "challenger")


@pytest.mark.parametrize("route", ["local", "commit", "pr"])
def test_prepare_delivers_deleted_file_comparison(tmp_path: Path, route: str) -> None:
    """Deliver exact deletion evidence even when the reviewed tip has no file record."""
    run = _review_inputs(tmp_path, deleted=route == "local")
    root = Path(json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"])
    head = None
    if route != "local":
        (root / "widget.py").unlink()
        subprocess.run(["git", "-C", str(root), "add", "widget.py"], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "Delete",
            ],
            check=True,
            capture_output=True,
        )
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        base = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD^"], check=True, capture_output=True, text=True
        ).stdout.strip()
        comparison = [f"{base}...{head}"] if route == "pr" else [base, head]
        (run / "diff.patch").write_bytes(
            subprocess.run(
                ["git", "-C", str(root), "diff", "--binary", *comparison, "--"], check=True, capture_output=True
            ).stdout
        )
        (run / "local-source/review-worktree.json").unlink()
        if route == "pr":
            (run / "local-checkout.json").write_text(
                json.dumps(
                    {"worktree": root.as_posix(), "expected_head": head, "diff_base_oid": base, "diff_head_oid": head}
                ),
                encoding="utf-8",
                newline="\n",
            )
    result = _prepare(run, source_root=root, expected_head=head)
    assert result.returncode == 0, result.stderr
    text = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    assert "widget.py (missing, SHA-256: None)" in text
    assert "deleted file mode" in text
    assert "-value = 1" in text
    assert not (root / "widget.py").exists()


@pytest.mark.parametrize("tampered", [False, True])
@pytest.mark.parametrize("scope_path", [None, "widget.py"])
def test_prepare_binds_preexisting_unchanged_caller(tmp_path: Path, tampered: bool, scope_path: str | None) -> None:
    """Admit HEAD-backed context while rejecting mutation after local collection."""
    run = _review_inputs(tmp_path, unchanged_caller=True)
    root = Path(json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"])
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"].append("stable.py")
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    if tampered:
        (root / "stable.py").write_text("caller = 999\n", encoding="utf-8", newline="\n")
    result = _prepare(run, scope_path=scope_path)
    if tampered:
        assert result.returncode != 0
        assert "changed after collection" in result.stderr
        assert not (run / "dispatch.json").exists()
    else:
        assert result.returncode == 0, result.stderr
        text = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
        assert "caller = 5\n" in text
        assert "+value = 2" in text


def test_prepare_rejects_undeclared_missing_context(tmp_path: Path) -> None:
    """Missing context requires a verified deletion rather than a nonexistent brief path."""
    run = _review_inputs(tmp_path)
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"].append("never-existed.py")
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode == 2
    assert "review-brief-source-selection-invalid:challenger" in result.stderr
    assert not (run / "dispatch.json").exists()


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """Write synthetic native rollout rows with portable line endings."""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def _record_native_schedule(
    run: Path,
    home: Path,
    children: dict[str, Path],
    *,
    pool_size: int = 4,
    scenario: str = "proper-refill",
    wave_index: int = 0,
) -> None:
    """Record coherent external allocation, task completion and final-answer join chronology."""
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    queue = [
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        for call in dispatch["calls"]
    ]
    actual = list(reversed(queue)) if scenario == "reversed-largest-order" else queue
    epoch = datetime(2026, 1, 1, 10, tzinfo=timezone.utc).timestamp() + wave_index * 1000
    launches = {}
    joins = {}
    active_ends = []
    paths = {}

    def stamp(value: float) -> str:
        """Serialize fixture timestamps using the host's UTC event format."""
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    for index, role in enumerate(actual):
        if index < pool_size:
            start = epoch + 0.2 + index / 10
            end = epoch + ([5, 5.01, 20, 25][index] if scenario == "blocked-wait-coalesces-joins" else 5 + index * 5)
            if scenario == "join-before-terminal" and index == 0:
                end = epoch + 8
        else:
            released = min(active_ends)
            active_ends.remove(released)
            start = (
                max(joins.values())
                if scenario in {"wait-for-all-before-refill", "wait-again-with-free-slot"}
                else released
            ) + 0.25
            if scenario == "blocked-wait-coalesces-joins":
                start = epoch + 15.3
            elif "wait-again-with-free-slot" in scenario:
                start = epoch + 45.3
            elif scenario == "join-before-terminal":
                start = epoch + 8.3
            end = start + 10
        launch = epoch + 0.05 + index / 100 if scenario == "spawn-all-five" else start - 0.02
        if scenario == "join-before-terminal" and index >= pool_size:
            launch = epoch + 5.28
        launches[role] = launch
        joins[role] = epoch + 5.05 if scenario == "join-before-terminal" and index == 0 else end + 0.05
        active_ends.append(joins[role])
        rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        rows[0]["payload"]["timestamp"] = stamp(launch + 0.005)
        paths[rows[0]["payload"]["agent_path"]] = role
        terminal = next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "task_complete")
        terminal.update(started_at=start, completed_at=end)
        _write_jsonl(children[role], rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    call_roles = {}
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
            arguments = json.loads(payload["arguments"])
            matches = [role for path, role in paths.items() if path.rsplit("/", 1)[-1] == arguments["task_name"]]
            if matches:
                role = matches[0]
                call_roles[payload["call_id"]] = role
                row["timestamp"] = stamp(launches[role])
        elif payload.get("type") == "function_call_output" and payload.get("call_id") in call_roles:
            row["timestamp"] = stamp(launches[call_roles[payload["call_id"]]] + 0.01)
        elif payload.get("type") == "agent_message" and payload.get("author") in paths:
            row["timestamp"] = stamp(joins[paths[payload["author"]]])
    if scenario in {"capacity-rejected", "capacity-rejected-repeat"}:
        # Existing rollout envelope, exact observed live refusal text. The host's
        # serialization of a failed spawn is not asserted by this parser fixture.
        targets = range(pool_size, len(actual)) if scenario == "capacity-rejected-repeat" else [pool_size]
        for index in targets:
            next_role = actual[index]
            successful = next(
                row
                for row in parent
                if row.get("payload", {}).get("type") == "function_call"
                and row["payload"].get("call_id") in call_roles
                and call_roles[row["payload"]["call_id"]] == next_role
            )
            refusal = json.loads(json.dumps(successful))
            refusal_id = f"capacity-refusal-{wave_index}-{index}"
            rejected_at = epoch + 1 if index == pool_size else launches[actual[index - 1]] + 0.5
            refusal["timestamp"] = stamp(rejected_at)
            refusal["payload"]["call_id"] = refusal_id
            parent.extend(
                [
                    refusal,
                    {
                        "type": "response_item",
                        "timestamp": stamp(rejected_at + 0.01),
                        "payload": {
                            "type": "function_call_output",
                            "call_id": refusal_id,
                            "output": "collab spawn failed: agent thread limit reached",
                        },
                    },
                ]
            )
    if scenario == "blocked-wait-coalesces-joins" or "wait-again-with-free-slot" in scenario:
        # A matched wait result marks parent resumption. Joins delivered while
        # this wait is pending alone do not establish scheduling opportunity.
        arguments = (
            {} if scenario.startswith("default-") else {"timeout_ms": 30000 if scenario.startswith("30s-") else 10000}
        )
        waits = [(4.9, 15.1)]
        if "wait-again-with-free-slot" in scenario:
            waits.append((15.2, 45.2))
        for index, (started, returned) in enumerate(waits):
            call_id = f"schedule-wait-{wave_index}-{index}"
            timed_out = not scenario.startswith("completed-") and not (
                index == 0 and scenario.startswith(("default-", "30s-"))
            )
            output = {"message": "Wait timed out." if timed_out else "Wait completed.", "timed_out": timed_out}
            parent.extend(
                [
                    {
                        "type": "response_item",
                        "timestamp": stamp(epoch + started),
                        "payload": {
                            "type": "function_call",
                            "name": "wait_agent",
                            "call_id": call_id,
                            "arguments": json.dumps(arguments),
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": stamp(epoch + returned),
                        "payload": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(output),
                        },
                    },
                ]
            )
    parent.sort(key=lambda row: row.get("timestamp", ""))
    _write_jsonl(parent_path, parent)


def _assembly_evidence(
    tmp_path: Path,
    *,
    malformed_continuation: bool = False,
    prepared_run: Path | None = None,
    home: Path | None = None,
    wave_index: int = 0,
    final_header: str = "complete",
    findings: dict[str, str] | None = None,
    blocking_counts: dict[str, int] | None = None,
    active_limit: int | None = None,
) -> tuple[Path, Path, dict[str, Path]]:
    """Record dispatched calls, real context-reader output, and completed child turns."""
    run = prepared_run if prepared_run is not None else _review_inputs(tmp_path)
    if prepared_run is None:
        prepared = _prepare(run)
        assert prepared.returncode == 0, prepared.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    if malformed_continuation:
        message = dispatch["calls"][0]["arguments"]["message"]
        dispatch["calls"][0]["arguments"]["message"] = message.replace(" --page 2", " --page WRONG", 1)
    home = home if home is not None else tmp_path / "codex-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    parent_path = sessions / "rollout-parent.jsonl"
    parent_rows: list[dict[str, object]] = (
        [json.loads(row) for row in parent_path.read_text(encoding="utf-8").splitlines()]
        if parent_path.exists()
        else [{"type": "session_meta", "payload": {"id": "parent"}}]
    )
    children = {}
    calls_by_role = {
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-"): call
        for call in dispatch["calls"]
    }
    for index, context in enumerate(plan["contexts"], start=1):
        role = context["role_id"]
        call = calls_by_role[role]
        arguments = call["arguments"]
        agent_path = f"/root/{arguments['task_name']}"
        identity = f"{wave_index}-{index}" if prepared_run is not None else str(index)
        thread = f"child-{identity}"
        turn = f"turn-{identity}"
        call_id = f"spawn-{identity}"
        message = arguments["message"]
        page_match = re.search(r"Read all (\d+) frozen review context pages", message)
        assert page_match is not None, "compact-review-page-count-invalid"
        page_count = int(page_match.group(1))
        if prepared_run is None:
            assert page_count > 1
        read_calls = re.findall(r"```javascript\n(.*?)\n```", message, flags=re.DOTALL)
        assert len(read_calls) == page_count, "review-dispatch-page-count-invalid"
        for page, read_call in enumerate(read_calls, 1):
            read_arguments = json.loads(read_call.split("tools.exec_command(", 1)[1].split("); text", 1)[0])
            assert read_arguments["cmd"].endswith("--attempt 1" if page == 1 else f"--page {page}"), (
                "review-dispatch-page-command-invalid"
            )
        tool_rows = []
        header = ""
        for page, read_call in enumerate(read_calls, start=1):
            output = _CONTEXT_READER(run / "inspection-plan.json", role, 1, page)
            if page == 1:
                header = output.splitlines()[0]
            read_id = f"read-{index}-{page}"
            tool_rows.extend(
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "exec",
                            "call_id": read_id,
                            "input": read_call,
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": read_id,
                            "output": [
                                {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
                                {"type": "input_text", "text": output},
                            ],
                        },
                    },
                ]
            )
        if final_header == "missing":
            header = ""
        elif final_header == "truncated":
            header = header.replace(f"input={plan['review_input_sha256']}", f"input={plan['review_input_sha256'][:-1]}")
        final = (
            f"{header}\nNo finding.\n\n## Reviewer Assessment\n\nRating: 1\nRationale: The inspected scope is clean."
        )
        if run.parent.name == "batches":
            final = '## Reviewer Findings\n```json\n[]\n```\n\n## Reviewer Confidence\n```json\n{"score": 0.95, "scope": "Frozen source and declared interactions.", "gaps": [{"gap": "Synthetic offline client.", "status": "unresolved", "rationale": "Fixture proves admission without a live semantic reviewer."}]}\n```\n\n## Reviewer Assessment\nRating: 1\nRationale: The inspected scope is clean.'
        if findings and role in findings:
            final = ("" if run.parent.name == "batches" else header + "\n") + findings[role]
        parent_rows.extend(
            [
                {
                    "timestamp": "2026-01-01T10:00:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "call_id": call_id,
                        "arguments": json.dumps(arguments),
                    },
                },
                {
                    "timestamp": "2026-01-01T10:00:00.100Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"task_name": agent_path}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "author": agent_path,
                        "content": [{"type": "input_text", "text": f"Message Type: FINAL_ANSWER\nPayload:\n{final}"}],
                    },
                },
            ]
        )
        child = sessions / f"rollout-{thread}.jsonl"
        _write_jsonl(
            child,
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread,
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
                    "payload": {"turn_id": turn, "model": arguments["model"], "effort": arguments["reasoning_effort"]},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "content": [{"type": "encrypted_content", "encrypted_content": arguments["message"]}],
                    },
                },
                *tool_rows,
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn,
                        "started_at": 100
                        + index
                        + wave_index * 1000
                        + ((index - 1) // active_limit * 20 if active_limit else 0),
                        "completed_at": 110
                        + index
                        + wave_index * 1000
                        + ((index - 1) // active_limit * 20 if active_limit else 0),
                        "last_agent_message": final,
                    },
                },
            ],
        )
        children[role] = child
    _write_jsonl(sessions / "rollout-parent.jsonl", parent_rows)
    _record_native_schedule(run, home, children, pool_size=active_limit or 4, wave_index=wave_index)
    (run / "specialist-assessments.json").write_text(
        json.dumps(
            {
                role: {
                    "confidence": 0.95,
                    "blocking_findings": blocking_counts.get(role, 0)
                    if blocking_counts is not None
                    else sum(
                        record["severity"] != "low"
                        for record in json.loads(re.search(r"```json\n(.*?)\n```", findings[role], re.DOTALL)[1])
                    )
                    if run.parent.name == "batches" and findings and role in findings and "```json\n" in findings[role]
                    else 0,
                }
                for role in children
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    return run, home, children


def _assemble(run: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the shipped assembly command against synthetic native sessions."""
    return subprocess.run(
        [sys.executable, str(HELPER), "assemble", "--out", str(run), "--codex-home", str(home)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("case", ["repair", "parent-miscopy", "source-read", "no-diagnostic", "wrong-parent"])
def test_same_wave_dispatch_repair_requires_observed_preassessment_failure(tmp_path: Path, case: str) -> None:
    """Recover a failed reader dispatch without dropping original allocation or valid sibling evidence."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=4)
    role = "sw-engineer"
    child = children[role]
    original_rows = [json.loads(line) for line in child.read_text().splitlines()]
    rows = json.loads(json.dumps(original_rows))
    tool_rows = [
        row
        for row in rows
        if row["type"] == "response_item"
        and row["payload"].get("type") in {"custom_tool_call", "custom_tool_call_output"}
    ]
    first_call = tool_rows[0]
    first_call["payload"]["input"] = first_call["payload"]["input"].replace("review_context.py", "review_prepare.py")
    output = tool_rows[1]
    if case != "source-read":
        output["payload"]["output"] = [
            {"type": "input_text", "text": "Script completed\n"},
            {
                "type": "input_text",
                "text": "usage: review_prepare.py\nerror: unsupported arguments"
                if case != "no-diagnostic"
                else "Unknown failure",
            },
        ]
    rows = [row for row in rows if row not in tool_rows] + [first_call, output]
    terminal = next(
        row["payload"] for row in rows if row["type"] == "event_msg" and row["payload"].get("type") == "task_complete"
    )
    blocked_message = "Reader dispatch failed before source inspection.\n\n## Reviewer Assessment\nRating: 5\nRationale: No frozen source pages were available."
    terminal["last_agent_message"] = blocked_message
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text().splitlines()]
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("type") == "agent_message" and payload.get("author") == rows[0]["payload"]["agent_path"]:
            payload["content"] = [
                {"type": "input_text", "text": f"Message Type: FINAL_ANSWER\nPayload:\n{blocked_message}"}
            ]
    _write_jsonl(parent_path, parent)
    if case == "wrong-parent":
        rows[0]["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = "unrelated"
    _write_jsonl(child, rows)
    if case == "parent-miscopy":
        parent_path = home / "sessions/rollout-parent.jsonl"
        parent = [json.loads(line) for line in parent_path.read_text().splitlines()]
        child_name = rows[0]["payload"]["agent_path"].rsplit("/", 1)[1]
        for row in parent:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                args = json.loads(payload["arguments"])
                if args["task_name"] == child_name:
                    # Only the first supplied block is malformed; later page commands remain canonical.
                    args["message"] = args["message"].replace("review_context.py", "review_prepare.py", 1)
                    payload["arguments"] = json.dumps(args)
                    for child_row in rows:
                        if child_row["type"] == "response_item" and child_row["payload"].get("type") == "agent_message":
                            child_row["payload"]["content"] = [
                                {"type": "encrypted_content", "encrypted_content": args["message"]}
                            ]
        _write_jsonl(parent_path, parent)
        _write_jsonl(child, rows)
    command = [
        sys.executable,
        str(HELPER),
        "prepare-repair",
        "--out",
        str(run),
        "--codex-home",
        str(home),
        "--role",
        role,
        "--kind",
        "incomplete-dispatch",
    ]
    prepared = subprocess.run(command, capture_output=True, text=True, check=False)
    if case not in {"repair", "parent-miscopy"}:
        assert prepared.returncode != 0
        assert (
            "source-already-read"
            if case == "source-read"
            else "failure-unproven"
            if case == "no-diagnostic"
            else "review-child-session-not-unique"
        ) in prepared.stderr
        return
    assert prepared.returncode == 0, prepared.stderr
    arguments = json.loads(prepared.stdout)["arguments"]
    original_path = original_rows[0]["payload"]["agent_path"]
    new_path = original_path.removesuffix("_a1") + "_a2"
    thread = "dispatch-repair-thread"
    original_thread = original_rows[0]["payload"]["id"]
    replacement = json.loads(
        json.dumps(original_rows)
        .replace(original_path, new_path)
        .replace(original_thread, thread)
        .replace("--attempt 1", "--attempt 2")
        .replace("attempt=1", "attempt=2")
    )
    replacement[0]["payload"]["timestamp"] = "2026-01-01T10:01:00.050Z"
    for row in replacement:
        if row["type"] == "response_item" and row["payload"].get("type") == "agent_message":
            row["payload"]["content"] = [{"type": "encrypted_content", "encrypted_content": arguments["message"]}]
    terminal = replacement[-1]["payload"]
    terminal.update(started_at=1767261660.2, completed_at=1767261661.0)
    _write_jsonl(home / f"sessions/rollout-{thread}.jsonl", replacement)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text().splitlines()]
    parent.extend(
        [
            {
                "timestamp": "2026-01-01T10:01:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "repair-spawn",
                    "arguments": json.dumps(arguments),
                },
            },
            {
                "timestamp": "2026-01-01T10:01:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "repair-spawn",
                    "output": json.dumps({"task_name": new_path}),
                },
            },
            {
                "timestamp": "2026-01-01T10:01:01.100Z",
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": new_path,
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Message Type: FINAL_ANSWER\nPayload:\n{terminal['last_agent_message']}",
                        }
                    ],
                },
            },
        ]
    )
    _write_jsonl(parent_path, parent)
    assembled = _assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text())
    repaired = next(item for item in manifest["passes"] if item["role"] == role)
    assert repaired["selected_attempt"] == 2
    assert len(repaired["attempts"]) == 2
    assert all(item["selected_attempt"] == 1 for item in manifest["passes"] if item["role"] != role)
    assert json.loads((run / "inspection-summary.json").read_text())["actual_mode"] == "parallel"


def test_prepare_freezes_complete_wave_and_keeps_source_out_of_dispatch(tmp_path: Path) -> None:
    """Bind each canonical role to its size-ordered call without embedding source in dispatch."""
    run = _review_inputs(tmp_path)
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    assert dispatch["routing_sha256"] == hashlib.sha256((run / "review-routing.json").read_bytes()).hexdigest()
    assert dispatch["briefs_sha256"] == hashlib.sha256((run / "review-briefs.json").read_bytes()).hexdigest()
    assert [entry["role_id"] for entry in plan["contexts"]] == ["challenger", "qa-specialist"]
    assert len(dispatch["calls"]) == 2
    queued_roles = [
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        for call in dispatch["calls"]
    ]
    sizes = {entry["role_id"]: (run / entry["context_path"]).stat().st_size for entry in plan["contexts"]}
    assert queued_roles == sorted(sizes, key=lambda role: (-sizes[role], role))
    calls_by_role = dict(zip(queued_roles, dispatch["calls"], strict=True))
    assert set(calls_by_role) == set(sizes)
    for entry in plan["contexts"]:
        context = (run / entry["context_path"]).read_bytes()
        role = entry["role_id"]
        call = calls_by_role[role]
        assert hashlib.sha256(context).hexdigest() == entry["context_sha256"]
        assert context.startswith((PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes())
        assert "Relevant source evidence." not in call["arguments"]["message"]
        assert call["arguments"]["fork_turns"] == "none"
        assert call["arguments"]["agent_type"] == "default"
        assert call["arguments"]["task_name"] == (f"review_{role.replace('-', '_')}_{entry['context_sha256'][:12]}_a1")
        page_count = len(runpy.run_path(str(SKILL / "review_context.py"))["context_pages"](context.decode("utf-8")))
        sources = re.findall(r"```javascript\n(.*?)\n```", call["arguments"]["message"], re.DOTALL)
        assert len(sources) == page_count
        for page, source in enumerate(sources, 1):
            command = json.loads(source.split("tools.exec_command(", 1)[1].split("); text", 1)[0])["cmd"]
            assert command.endswith("--attempt 1" if page == 1 else f"--page {page}")
        assert "append ` --page N`" not in call["arguments"]["message"]
    assert dispatch["context_bytes"] > dispatch["dispatch_bytes"]


def test_prepare_rejects_missing_role_before_freezing_any_context(tmp_path: Path) -> None:
    run = _review_inputs(tmp_path)
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs.pop("challenger")
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode != 0
    assert "review-brief-role-set-mismatch" in result.stderr
    assert not (run / "inspection-plan.json").exists()


def test_prepare_rejects_prose_only_source_claim_before_freezing(tmp_path: Path) -> None:
    """A claimed inspection cannot substitute for source bytes from the collected checkout."""
    run = _review_inputs(tmp_path)
    (run / "challenger-evidence.md").write_text(
        "I inspected widget.py at the frozen revision.\n", encoding="utf-8", newline="\n"
    )
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"].pop("source_paths")
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode != 0
    assert "review-brief-source-selection-invalid:challenger" in result.stderr
    assert not (run / "inspection-plan.json").exists()
    assert not (run / "specialists").exists()


def test_prepare_includes_exact_untracked_source(tmp_path: Path) -> None:
    """An untracked file has exact source bytes even though Git has no diff hunk for it."""
    run = _review_inputs(tmp_path, untracked=True)
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"] = ["new.txt"]
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    context = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    assert "new_value = 7\n" in context
    assert "Untracked file: exact source bytes" in context
    assert "value = 2\n" not in context


@pytest.mark.parametrize("forged_diff", [False, True])
def test_prepare_accepts_only_matching_committed_detached_source(tmp_path: Path, forged_diff: bool) -> None:
    """Bind a committed review to the declared exact HEAD and selected source bytes."""
    run = _review_inputs(tmp_path)
    review = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    )
    source = review / "widget.py"
    source.write_text("value = 3\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(review), "add", "widget.py"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(review),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Second",
        ],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(review), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "-C", str(review), "diff", "--binary", "HEAD^", "HEAD"], check=True, capture_output=True
    ).stdout
    if forged_diff:
        diff = diff.replace(b"+value = 3", b"+value = 999")
    (run / "diff.patch").write_bytes(diff)
    (run / "local-source/review-worktree.json").unlink()
    result = _prepare(run, source_root=review, expected_head=head)
    if forged_diff:
        assert result.returncode != 0
        assert "review-source-diff-stale" in result.stderr
        assert not (run / "inspection-plan.json").exists()
        return
    assert result.returncode == 0, result.stderr
    context = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    assert "value = 3\n" in context
    assert "+value = 3" in context


@pytest.mark.parametrize(
    ("destination", "rename"),
    [
        pytest.param("new.py", True, id="plain-rename"),
        pytest.param("café.py", True, id="quoted-rename"),
        pytest.param("café.py", False, id="quoted-update"),
    ],
)
def test_prepare_delivers_committed_destination(tmp_path: Path, destination: str, rename: bool) -> None:
    """Deliver exact source and patch bytes for renamed and quoted Git paths."""
    run = _review_inputs(tmp_path)
    review = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    )
    (review / "widget.py").write_text("value = 1\n", encoding="utf-8", newline="\n")
    original = "old.py" if rename else destination
    (review / original).write_text("def renamed():\n    return 17\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(review), "add", original], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(review),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Base",
        ],
        check=True,
        capture_output=True,
    )
    if rename:
        subprocess.run(["git", "-C", str(review), "mv", original, destination], check=True, capture_output=True)
    else:
        (review / destination).write_text("def renamed():\n    return 18\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "-C", str(review), "add", destination], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(review),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Rename",
        ],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(review), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    patch = subprocess.run(
        ["git", "-C", str(review), "diff", "--binary", "HEAD^", "HEAD"], check=True, capture_output=True
    ).stdout
    assert (b"rename to " in patch) is rename
    (run / "diff.patch").write_bytes(patch)
    (run / "local-source/review-worktree.json").unlink()
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    for brief in briefs.values():
        brief["source_paths"] = [destination]
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")

    result = _prepare(run, source_root=review, expected_head=head)

    assert result.returncode == 0, result.stderr
    context = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    assert destination in context
    assert f"return {17 if rename else 18}" in context
    assert ("rename to " in context) is rename
    assert (run / "dispatch.json").exists()


def test_prepare_uses_nested_pr_receipt_and_rejects_ambiguous_receipts(tmp_path: Path) -> None:
    """Resolve the retained PR receipt at its actual path and reject competing receipts."""
    run = _review_inputs(tmp_path)
    review = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    )
    (review / "widget.py").write_text("value = 3\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(review), "add", "widget.py"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(review),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Second",
        ],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(review), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    base = subprocess.run(
        ["git", "-C", str(review), "rev-parse", "HEAD^"], check=True, capture_output=True, text=True
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "-C", str(review), "diff", "--binary", f"{base}...{head}", "--"], check=True, capture_output=True
    ).stdout
    (run / "diff.patch").write_bytes(diff)
    (run / "local-source/review-worktree.json").unlink()
    (run / "pr").mkdir()
    receipt = {"worktree": review.as_posix(), "expected_head": head, "diff_base_oid": base, "diff_head_oid": head}
    (run / "pr/local-checkout.json").write_text(json.dumps(receipt), encoding="utf-8", newline="\n")
    result = _prepare(run, source_root=review)
    assert result.returncode == 0, result.stderr
    assert "value = 3\n" in (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    (run / "local-checkout.json").write_text(json.dumps(receipt), encoding="utf-8", newline="\n")
    result = _prepare(run, source_root=review)
    assert result.returncode != 0
    assert "review-source-receipt-ambiguous" in result.stderr


def test_prepare_rejects_source_drift_before_freezing(tmp_path: Path) -> None:
    """A modified isolated checkout invalidates the retained source snapshot."""
    run = _review_inputs(tmp_path)
    review = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    )
    (review / "widget.py").write_text("value = 99\n", encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode != 0
    assert "Review worktree source changed after collection" in result.stderr
    assert not (run / "inspection-plan.json").exists()
    assert not (run / "specialists").exists()


def test_prepare_rejects_unrelated_source_selection(tmp_path: Path) -> None:
    """An unchanged selected path cannot stand alone as review evidence."""
    run = _review_inputs(tmp_path)
    repository = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["source_worktree"]
    )
    (repository / "unchanged.py").write_text("stable = True\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(repository), "add", "unchanged.py"], check=True, capture_output=True)
    # Selection outside the collector's frozen source inventory must fail closed.
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"] = ["unchanged.py"]
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode != 0
    assert (
        "review-brief-source-unrelated:challenger" in result.stderr
        or "review-brief-source-selection-invalid:challenger" in result.stderr
    )
    assert not (run / "inspection-plan.json").exists()


def test_prepare_pages_context_larger_than_old_limit(tmp_path: Path) -> None:
    """Admit a complete large context through the bounded native page reader."""
    run = _review_inputs(tmp_path)
    (run / "challenger-evidence.md").write_text("bounded evidence\n" * 5000, encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    context = (run / "specialists/challenger-context.md").read_bytes()
    assert len(context) > 65536
    assert context.count(b"bounded evidence\n") == 5000
    assert (run / "inspection-plan.json").exists()


def test_prepare_rejects_oversized_context_before_freezing_wave(tmp_path: Path) -> None:
    """Keep a context above the bounded native read ceiling out of the frozen wave."""
    run = _review_inputs(tmp_path)
    (run / "challenger-evidence.md").write_text("bounded evidence\n" * 18000, encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode != 0
    assert "review-context-capacity-exceeded:challenger:262144-bytes" in result.stderr
    assert not (run / "inspection-plan.json").exists()
    assert not (run / "specialists").exists()


@pytest.mark.parametrize("problem", ["changed-brief", "sensitive-evidence"])
def test_prepare_never_overwrites_frozen_evidence_or_retains_secrets(tmp_path: Path, problem: str) -> None:
    run = _review_inputs(tmp_path)
    if problem == "changed-brief":
        assert _prepare(run).returncode == 0
        original = (run / "inspection-plan.json").read_bytes()
        (run / "qa-specialist-evidence.md").write_text("Different source.", encoding="utf-8", newline="\n")
        expected = "review-frozen-artifact-conflict"
    else:
        (run / "qa-specialist-evidence.md").write_text(
            "Authorization: Bearer do-not-retain", encoding="utf-8", newline="\n"
        )
        expected = "review-context-sensitive-material"
    result = _prepare(run)
    assert result.returncode != 0
    assert expected in result.stderr
    if problem == "changed-brief":
        assert (run / "inspection-plan.json").read_bytes() == original
    else:
        assert not (run / "specialists").exists()


def test_assemble_binds_native_wave_and_preserves_child_outputs(tmp_path: Path, text_newline_default: None) -> None:
    """Accept a completed, overlapping wave with outputs bound to child finals."""
    run, home, children = _assembly_evidence(tmp_path)
    # Native history also contains non-spawn subagents; unrelated session shapes must not abort this wave.
    _write_jsonl(
        home / "sessions" / "unrelated.jsonl",
        [{"type": "session_meta", "payload": {"id": "unrelated", "source": {"subagent": "compact"}}}],
    )
    result = _assemble(run, home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 8
    assert manifest["manifest_kind"] == "native-wave"
    assert summary["actual_mode"] == "parallel"
    assert {item["role"] for item in manifest["passes"]} == set(children)
    for item in manifest["passes"]:
        role = item["role"]
        rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        final = rows[-1]["payload"]["last_agent_message"]
        assert (run / item["output_path"]).read_text(encoding="utf-8") == final + "\n"
        assert item["attempts"][0]["agent_thread_id"] == rows[0]["payload"]["id"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "missing-delivery",
        "different-delivery",
        "duplicate-delivery",
        "extra-argument",
        "missing-argument",
        "changed-model",
        "page",
        "later-page",
        "boolean-blockers",
    ],
)
def test_native_assembly_binds_opaque_delivery_to_exact_audited_execution(tmp_path: Path, damage: str) -> None:
    """Admit transported messages only with intact child delivery, exact controls, and source reads."""
    run, home, children = _assembly_evidence(tmp_path)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent_rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    for row in parent_rows:
        payload = row.get("payload", {})
        if payload.get("type") != "function_call" or payload.get("name") != "spawn_agent":
            continue
        arguments = json.loads(payload["arguments"])
        role = arguments["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        opaque = arguments["message"] if damage == "boolean-blockers" else "opaque-host-transport:" + role
        arguments["message"] = opaque
        if role == "challenger" and damage == "extra-argument":
            arguments["unsupported"] = True
        if role == "challenger" and damage == "missing-argument":
            del arguments["fork_turns"]
        if role == "challenger" and damage == "changed-model":
            arguments["model"] = "unselected-model"
        payload["arguments"] = json.dumps(arguments)
        child_rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        delivery = next(row for row in child_rows if row.get("payload", {}).get("type") == "agent_message")
        delivery["payload"]["content"] = [{"type": "encrypted_content", "encrypted_content": opaque}]
        if role == "challenger":
            if damage == "missing-delivery":
                child_rows.remove(delivery)
            elif damage == "different-delivery":
                delivery["payload"]["content"][0]["encrypted_content"] = "another-transport"
            elif damage == "duplicate-delivery":
                child_rows.insert(child_rows.index(delivery), delivery)
            elif damage in {"page", "later-page"}:
                page_calls = [row for row in child_rows if row.get("payload", {}).get("type") == "custom_tool_call"]
                call = page_calls[0 if damage == "page" else 1]
                call["payload"]["input"] += "\nUnexpected execution."
        _write_jsonl(children[role], child_rows)
    _write_jsonl(parent_path, parent_rows)
    if damage == "boolean-blockers":
        path = run / "specialist-assessments.json"
        assessments = json.loads(path.read_bytes())
        assessments["challenger"]["blocking_findings"] = False
        path.write_text(json.dumps(assessments), encoding="utf-8", newline="\n")
    before = {path: path.read_bytes() for path in (parent_path, *children.values(), run / "dispatch.json")}
    result = _assemble(run, home)
    if damage == "none":
        assert result.returncode == 0, result.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        for item in manifest["passes"]:
            terminal = json.loads(before[children[item["role"]]].splitlines()[-1])["payload"]["last_agent_message"]
            assert (run / item["attempts"][0]["raw_output_path"]).read_bytes() == terminal.encode("utf-8")
    else:
        assert result.returncode != 0
        assert not (run / "specialist-manifest.json").exists()
        if damage == "boolean-blockers":
            assert "manifest-invalid-blocking-findings:challenger" in result.stderr
        elif damage in {"page", "later-page"}:
            page = 1 if damage == "page" else 2
            assert f"review-inspection-context-read-call-mismatch:challenger:{page}" in result.stderr
        elif damage in {
            "missing-delivery",
            "different-delivery",
            "duplicate-delivery",
            "missing-argument",
            "changed-model",
        }:
            assert "provenance-parent-spawn-mismatch:challenger:1" in result.stderr
        else:
            assert "review-inspection-launch-arguments-invalid:challenger:1" in result.stderr
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "start", ["same-second-integer", "previous-second-integer", "prior-fractional-float", "prior-adjacent-float"]
)
def test_native_assembly_respects_observed_start_timestamp_precision(tmp_path: Path, start: str) -> None:
    """Admit overlapping integer-second start buckets while rejecting provably earlier starts."""
    run, home, children = _assembly_evidence(tmp_path)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent_rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    child_path = children["challenger"]
    child_rows = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    agent_path = child_rows[0]["payload"]["agent_path"]
    launch = next(
        row
        for row in parent_rows
        if row.get("payload", {}).get("name") == "spawn_agent"
        and json.loads(row["payload"]["arguments"])["task_name"] == agent_path.rsplit("/", 1)[-1]
    )
    launched_at = datetime.fromisoformat(launch["timestamp"].replace("Z", "+00:00")).timestamp()
    assert launched_at != int(launched_at)
    terminal = next(row["payload"] for row in child_rows if row.get("payload", {}).get("type") == "task_complete")
    terminal["started_at"] = {
        "same-second-integer": int(launched_at),
        "previous-second-integer": int(launched_at) - 1,
        "prior-fractional-float": launched_at - 0.001,
        "prior-adjacent-float": math.nextafter(launched_at, -math.inf),
    }[start]
    if start == "prior-adjacent-float":
        assert datetime.fromtimestamp(terminal["started_at"], timezone.utc) == datetime.fromtimestamp(
            launched_at, timezone.utc
        )
    _write_jsonl(child_path, child_rows)
    original = child_path.read_bytes()
    result = _assemble(run, home)
    if start == "same-second-integer":
        assert result.returncode == 0, result.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        assert {item["role"] for item in manifest["passes"]} == set(children)
    else:
        assert result.returncode != 0
        assert "review-inspection-parent-join-missing:challenger" in result.stderr
        assert not (run / "specialist-manifest.json").exists()
    assert child_path.read_bytes() == original


@pytest.mark.integration
@pytest.mark.parametrize("wait", ["prior-wave", "spanning-first-launch"])
def test_native_assembly_scopes_refill_checks_to_current_wave_dispatch(tmp_path: Path, wait: str) -> None:
    """Preserve earlier wave waits without admitting a wait that spans the next wave's launch."""
    run, home, _children = _assembly_evidence(tmp_path)
    parent_path = home / "sessions/rollout-parent.jsonl"
    rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    returned = "2026-01-01T09:59:59Z" if wait == "prior-wave" else "2026-01-01T10:00:00.200Z"
    historical = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T09:59:58Z",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": "earlier-wave-wait",
                "arguments": json.dumps({"timeout_ms": 10000}),
            },
        },
        {
            "type": "response_item",
            "timestamp": returned,
            "payload": {
                "type": "function_call_output",
                "call_id": "earlier-wave-wait",
                "output": json.dumps({"timed_out": False}),
            },
        },
    ]
    rows[1:1] = historical
    _write_jsonl(parent_path, rows)
    original = parent_path.read_bytes()
    result = _assemble(run, home)
    if wait == "prior-wave":
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "review-inspection-schedule-wait-invalid" in result.stderr
        assert not (run / "specialist-manifest.json").exists()
    assert parent_path.read_bytes() == original


def _retain_legacy_native_recipe(
    run: Path,
    home: Path,
    children: dict[str, Path],
    *,
    provenance_header: bool,
    reader_fixture: str = "legacy-context-reader",
) -> Path:
    """Retain the known historical reader and its issued messages with exact page call coordinates."""
    reader_path = Path(__file__).with_name("fixtures") / reader_fixture / "review_context.py"
    expected_digest = {
        "legacy-context-reader": "47024ba02c7dec6927356dca33ec44e9710de325fcc1fbb8ec56f66ad0a9c772",
        "legacy-all-page-context-reader": "c185dc007a261a2d9c0e449e5a888a7a771cd99336ebe8c7085432bc2b0d43c8",
    }[reader_fixture]
    assert hashlib.sha256(reader_path.read_bytes()).hexdigest() == expected_digest
    legacy = runpy.run_path(str(reader_path))
    dispatch_path = run / "dispatch.json"
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent_rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    for call in dispatch["calls"]:
        arguments = call["arguments"]
        role = arguments["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        message = legacy["dispatch_message"](
            run / "inspection-plan.json",
            role,
            1,
            dispatch["context_reader_python"],
            provenance_header=provenance_header,
            reader_path=reader_path,
        )
        arguments["message"] = message
        for row in parent_rows:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                sent = json.loads(payload["arguments"])
                if sent["task_name"] == arguments["task_name"]:
                    sent["message"] = message
                    payload["arguments"] = json.dumps(sent)
        child_rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        page = 0
        for row in child_rows:
            payload = row.get("payload", {})
            if payload.get("type") == "agent_message":
                payload["content"][0]["encrypted_content"] = message
            if payload.get("type") == "custom_tool_call":
                page += 1
                payload["input"] = legacy["render_read_call"](
                    run / "inspection-plan.json",
                    role,
                    1,
                    dispatch["context_reader_python"],
                    page,
                    reader_path,
                )
        _write_jsonl(children[role], child_rows)
    dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8", newline="\n")
    _write_jsonl(parent_path, parent_rows)
    return reader_path


@pytest.mark.integration
@pytest.mark.parametrize("protocol", ["paged-context-v6", "paged-context-v7"])
@pytest.mark.parametrize("reader_fixture", ["legacy-context-reader", "legacy-all-page-context-reader"])
@pytest.mark.parametrize("tampered_reader", [False, True])
def test_manifest_consumer_preserves_known_historical_reader_recipe(
    tmp_path: Path, protocol: str, reader_fixture: str, tampered_reader: bool
) -> None:
    """Admit issued historical page recipes while rejecting unknown reader bytes at consumer intake."""
    run, home, children = _assembly_evidence(tmp_path)
    assembled = _assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    reader_path = _retain_legacy_native_recipe(
        run, home, children, provenance_header=protocol == "paged-context-v6", reader_fixture=reader_fixture
    )
    manifest_path = run / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest.update(
        dispatch_protocol=protocol,
        context_reader_path=str(reader_path.resolve()),
        context_reader_sha256=hashlib.sha256(reader_path.read_bytes()).hexdigest(),
    )
    if tampered_reader:
        reader_path = tmp_path / "unknown-reader" / "review_context.py"
        reader_path.parent.mkdir()
        reader_path.write_bytes(Path(manifest["context_reader_path"]).read_bytes() + b"\n# altered reader\n")
        manifest.update(
            context_reader_path=str(reader_path),
            context_reader_sha256=hashlib.sha256(reader_path.read_bytes()).hexdigest(),
        )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    retained = {path: path.read_bytes() for path in [manifest_path, *children.values(), *run.glob("*.raw.md")]}
    result = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
            "--out",
            str(run),
            "--manifest-only",
            "--codex-home",
            str(home),
            "--parent-thread-id",
            "parent",
            "--project-root",
            str(PLUGIN_ROOT.parents[1]),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == int(tampered_reader), result.stderr
    if tampered_reader:
        assert "manifest-context-reader-identity-invalid" in result.stderr
    assert {path: path.read_bytes() for path in retained} == retained


@pytest.mark.integration
@pytest.mark.parametrize("reader_fixture", ["current", "legacy-all-page-context-reader"])
def test_manifest_consumer_rejects_workdir_from_different_reader_recipe(tmp_path: Path, reader_fixture: str) -> None:
    """Keep current cwd-free calls and historical cwd-bearing calls distinct at consumer intake."""
    run, home, children = _assembly_evidence(tmp_path)
    assembled = _assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    manifest_path = run / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if reader_fixture != "current":
        reader_path = _retain_legacy_native_recipe(
            run, home, children, provenance_header=False, reader_fixture=reader_fixture
        )
        manifest.update(
            context_reader_path=str(reader_path.resolve()),
            context_reader_sha256=hashlib.sha256(reader_path.read_bytes()).hexdigest(),
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    child_path = children["qa-specialist"]
    rows = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    call = next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call")
    arguments = json.loads(call["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
    if reader_fixture == "current":
        assert "workdir" not in arguments
        arguments["workdir"] = str(run)
    else:
        assert arguments.pop("workdir") == str(run.resolve())
    call["input"] = (
        '// @exec: {"max_output_tokens": 10000}\n'
        f"const r = await tools.exec_command({json.dumps(arguments, ensure_ascii=False)}); text(r.output);"
    )
    _write_jsonl(child_path, rows)
    retained = {path: path.read_bytes() for path in [manifest_path, *children.values()]}
    result = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
            "--out",
            str(run),
            "--manifest-only",
            "--codex-home",
            str(home),
            "--parent-thread-id",
            "parent",
            "--project-root",
            str(PLUGIN_ROOT.parents[1]),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "review-inspection-context-read-call-mismatch:qa-specialist:1" in result.stderr
    assert {path: path.read_bytes() for path in retained} == retained


@pytest.mark.parametrize("operation", ["assemble", "recover"])
def test_native_producer_admits_runtime_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    """Count full runtime admission in the ordinary and recovery producers."""
    run, home, children = _assembly_evidence(tmp_path)
    import review_prepare

    if operation == "recover":
        reader_path = _retain_legacy_native_recipe(run, home, children, provenance_header=True)

    validator = review_prepare.validator
    original = validator._validate_review_runtime
    calls = []

    def counted_runtime(*args: object, **kwargs: object) -> dict[str, object]:
        """Preserve actual native validation while counting requests."""
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(validator, "_validate_review_runtime", counted_runtime)
    if operation == "assemble":
        summary = review_prepare.assemble(run, home)
        assert summary == json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    else:
        recovered = review_prepare.recover_native_provenance(run, home, reader_path)
        summary = recovered["summary"]
        assert summary == json.loads((run / "native-recovery/inspection-summary.json").read_text(encoding="utf-8"))
    assert summary["actual_mode"] == "parallel"
    assert len(calls) == 1


def test_assembly_fixture_rejects_malformed_compact_continuation(tmp_path: Path) -> None:
    """Reject a malformed supplied page command instead of synthesizing a correct continuation."""
    with pytest.raises(AssertionError, match="review-dispatch-page-command-invalid"):
        _assembly_evidence(tmp_path, malformed_continuation=True)


def test_assemble_rejects_unparseable_rating_before_promoting_manifest(tmp_path: Path) -> None:
    """Catch a malformed model assessment before downstream result writing fails."""
    run, home, children = _assembly_evidence(tmp_path)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    rows[-1]["payload"]["last_agent_message"] = rows[-1]["payload"]["last_agent_message"].replace(
        "Rating: 1", "Rating: 5/5"
    )
    _write_jsonl(child, rows)
    result = _assemble(run, home)
    assert result.returncode != 0
    assert "review-assessment-content-invalid:challenger" in result.stderr
    assert not (run / "specialist-manifest.json").exists()


def test_retained_rating_rejects_duplicate_reviewer_assessment(tmp_path: Path) -> None:
    """A second assessment must not hide a conflicting rating behind the first one."""
    spec = importlib.util.spec_from_file_location("rating_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    response = tmp_path / "response.md"
    response.write_text(
        "## Reviewer Assessment\n\nRating: 1\nRationale: Looks clean.\n\n"
        "## Reviewer Assessment\n\nRating: 5/5\nRationale: Conflicting.\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(SystemExit, match="review-assessment-content-invalid:challenger"):
        module._retained_reviewer_rating(response, local_reviewer_wave=False, main=False, role="challenger")


def test_pr_pass_import_proof_rejects_missing_test_origins(tmp_path: Path) -> None:
    """A passing PR test gate must prove selected test files came from the reviewed tree."""
    spec = importlib.util.spec_from_file_location("pr_proof_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    worktree = tmp_path / "review-worktree"
    worktree.mkdir()
    origin = (worktree / "package.py").as_posix()
    proof = {
        "mode": "in-process-pytest",
        "status": "pass",
        "worktree": worktree.as_posix(),
        "invoked_interpreter": sys.executable,
        "runtime_interpreter": sys.executable,
        "sys_prefix": sys.prefix,
        "modules": {"package": {"status": "pass", "tracked": True, "reason": None, "origin": origin}},
    }
    with pytest.raises(SystemExit, match="pr-source-review-tests-import-proof-invalid"):
        module._validate_pr_tests_import_proof(
            [{"id": "tests", "status": "pass", "python_imports": proof}], worktree.as_posix()
        )
    selected = (worktree / "test_package.py").as_posix()
    proof["tests"] = {selected: {"status": "pass", "tracked": True, "reason": None, "origin": selected}}
    module._validate_pr_tests_import_proof(
        [{"id": "tests", "status": "pass", "python_imports": proof}], worktree.as_posix()
    )
    proof["tests"][selected]["tracked"] = False
    with pytest.raises(SystemExit, match="pr-source-review-tests-import-proof-invalid"):
        module._validate_pr_tests_import_proof(
            [{"id": "tests", "status": "pass", "python_imports": proof}], worktree.as_posix()
        )
    proof["tests"] = {
        (tmp_path / "outside.py").as_posix(): {
            "status": "pass",
            "tracked": True,
            "reason": None,
            "origin": (tmp_path / "outside.py").as_posix(),
        }
    }
    with pytest.raises(SystemExit, match="pr-source-review-tests-import-proof-invalid"):
        module._validate_pr_tests_import_proof(
            [{"id": "tests", "status": "pass", "python_imports": proof}], worktree.as_posix()
        )


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        pytest.param("review-briefs.json", "review-prepared-briefs-changed", id="changed-axis"),
        pytest.param("review-routing.json", "review-prepared-routing-changed", id="changed-trigger"),
    ],
)
def test_assemble_rejects_changed_preparation_semantics(tmp_path: Path, file_name: str, expected: str) -> None:
    """Do not label frozen child findings with an axis or trigger edited after dispatch."""
    run, home, _ = _assembly_evidence(tmp_path)
    path = run / file_name
    payload = json.loads(path.read_text(encoding="utf-8"))
    if file_name == "review-briefs.json":
        payload["challenger"]["axis"] = "changed axis after dispatch"
    else:
        payload["trigger_reasons"]["challenger"] = ["Changed trigger after dispatch."]
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")

    result = _assemble(run, home)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (run / "specialist-manifest.json").exists()


@pytest.mark.parametrize(
    "problem, expected",
    [
        pytest.param("missing-child", "review-child-session-not-unique", id="missing-child"),
        pytest.param("wrong-receipt", "review-inspection-context-read-output-mismatch", id="mismatched-read-receipt"),
        pytest.param("extra-tool", "review-inspection-context-read-count-mismatch", id="extra-child-tool"),
        pytest.param("wrong-model", "provenance-role-model-policy-mismatch", id="wrong-child-model"),
        pytest.param("wrong-output", "review-inspection-parent-join-missing", id="wrong-joined-output"),
        pytest.param("no-overlap", "review-wave-not-parallel", id="nonoverlapping-wave"),
    ],
)
def test_assemble_rejects_unbound_or_incomplete_wave(
    tmp_path: Path, problem: str, expected: str, text_newline_default: None
) -> None:
    """Reject plausible rollout tampering instead of accepting a fabricated pass."""
    run, home, children = _assembly_evidence(tmp_path)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    parent = home / "sessions" / "rollout-parent.jsonl"
    parent_rows = [json.loads(line) for line in parent.read_text(encoding="utf-8").splitlines()]
    if problem == "missing-child":
        child.unlink()
    elif problem == "wrong-receipt":
        receipt = next(row for row in rows if row["payload"].get("type") == "custom_tool_call_output")
        receipt["payload"]["output"][1]["text"] = "forged context"
    elif problem == "extra-tool":
        rows.insert(
            -1,
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "unexpected",
                    "input": "text('extra')",
                },
            },
        )
    elif problem == "wrong-model":
        rows[1]["payload"]["model"] = "unrequested-model"
    elif problem == "wrong-output":
        dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
        challenger = next(call for call in dispatch["calls"] if call["role"] == "challenger")
        joined = next(
            row
            for row in parent_rows
            if row["payload"].get("author") == f"/root/{challenger['arguments']['task_name']}"
        )
        joined["payload"]["content"][0]["text"] += "\nforged final"
    else:
        _record_native_schedule(run, home, children, pool_size=1)
        rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
        parent_rows = [json.loads(line) for line in parent.read_text(encoding="utf-8").splitlines()]
    if problem != "missing-child":
        _write_jsonl(child, rows)
    _write_jsonl(parent, parent_rows)
    result = _assemble(run, home)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (run / "specialist-manifest.json").exists()
    assert not (run / "inspection-summary.json").exists()


@pytest.mark.parametrize(
    "page, expected_body",
    [pytest.param(1, b"\r\n" * 3000, id="crlf-page"), pytest.param(2, "é".encode("utf-8"), id="utf8-tail")],
)
def test_context_reader_preserves_native_stdout_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: int, expected_body: bytes
) -> None:
    """Dispatch and native stdout preserve frozen CRLF and Unicode across a byte boundary."""
    content = b"\r\n" * 3000 + "é".encode("utf-8")
    (tmp_path / "context.md").write_bytes(content)
    plan = tmp_path / "inspection-plan.json"
    plan.write_text(
        json.dumps(
            {
                "review_run_id": "portable-context",
                "review_input_sha256": "a" * 64,
                "contexts": [
                    {
                        "role_id": "challenger",
                        "context_path": "context.md",
                        "context_sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    reader = runpy.run_path(str(SKILL / "review_context.py"))
    assert "Read all 2 frozen review context pages" in reader["dispatch_message"](plan, "challenger")
    output = io.BytesIO()
    native_stdout = io.TextIOWrapper(output, encoding="cp1252", newline="\r\n")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", native_stdout)
        patch.setattr(
            sys,
            "argv",
            ["review_context.py", "--plan", str(plan), "--role", "challenger", "--attempt", "1", "--page", str(page)],
        )
        runpy.run_path(str(SKILL / "review_context.py"), run_name="__main__")
        native_stdout.flush()
    actual = output.getvalue()
    expected = reader["read_context"](plan, "challenger", 1, page).encode("utf-8")
    cli = subprocess.run(
        [
            sys.executable,
            str(SKILL / "review_context.py"),
            "--plan",
            str(plan),
            "--role",
            "challenger",
            "--attempt",
            "1",
            "--page",
            str(page),
        ],
        capture_output=True,
        check=True,
    )
    assert cli.stdout == actual == expected
    assert actual.endswith(expected_body)
    assert b"\r\r\n" not in actual
    assert f"codex-review-context-page {page}/2".encode() in actual


def test_batch_module_imports_without_sibling_path_in_importlib_collection(tmp_path: Path) -> None:
    """Root doctest discovery can import the helper by path without preloading sibling modules."""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import importlib.util, sys; spec = importlib.util.spec_from_file_location('collected_batches', sys.argv[1]); module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); assert module.CONTEXT_LIMIT == 65536",
            str(SKILL / "review_batches.py"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _five_role_review_inputs(tmp_path: Path) -> Path:
    """Declare five required axes on the existing exact-source public preparation fixture."""
    run = _review_inputs(tmp_path)
    roles = ["challenger", "data-steward", "doc-scribe", "qa-specialist", "sw-engineer"]
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing["signals"].update(behavior_change=True, axis_data_steward=True, axis_doc_scribe=True)
    routing.update(triggered_roles=roles, trigger_reasons={role: ["Declared source review axis."] for role in roles})
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    (run / "files.txt").write_text("widget.py\n", encoding="utf-8", newline="\n")
    briefs = {}
    for index, role in enumerate(roles, 1):
        evidence = f"{role}-evidence.md"
        (run / evidence).write_text(
            f"Scope: widget.py. Question: {role}.\n" + "Evidence.\n" * (index * 100), encoding="utf-8", newline="\n"
        )
        briefs[role] = {"axis": role, "evidence_path": evidence, "source_paths": ["widget.py"]}
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    return run


def test_prepare_five_roles_orders_complete_contexts_and_stable_ties(tmp_path: Path) -> None:
    """Queue the entire roster by descending frozen size, retaining deterministic equal-size order."""
    first = tmp_path / "first-a"
    second = tmp_path / "first-b"
    first.mkdir()
    second.mkdir()
    run = _five_role_review_inputs(first)
    prepared = _prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    first_sizes = {entry["role_id"]: (run / entry["context_path"]).stat().st_size for entry in plan["contexts"]}
    first_dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    assert [
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        for call in first_dispatch["calls"]
    ] == sorted(first_sizes, key=lambda role: (-first_sizes[role], role))
    target = max(first_sizes.values()) + 100
    tied = _five_role_review_inputs(second)
    for entry in plan["contexts"]:
        role = entry["role_id"]
        evidence = tied / f"{role}-evidence.md"
        evidence.write_bytes(evidence.read_bytes() + b"x" * (target - (run / entry["context_path"]).stat().st_size))
    completed = _prepare(tied)
    assert completed.returncode == 0, completed.stderr
    tied_plan = json.loads((tied / "inspection-plan.json").read_text(encoding="utf-8"))
    entries = tied_plan["contexts"]
    sizes = {entry["role_id"]: (tied / entry["context_path"]).stat().st_size for entry in entries}
    assert len(set(sizes.values())) == 1, sizes
    expected = sorted(sizes, key=lambda role: (-sizes[role], role))
    assert [entry["role_id"] for entry in entries] == expected
    dispatch = json.loads((tied / "dispatch.json").read_text(encoding="utf-8"))
    assert len(dispatch["calls"]) == 5
    assert [
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        for call in dispatch["calls"]
    ] == expected
    frozen_dispatch = (tied / "dispatch.json").read_bytes()
    assert _prepare(tied).returncode == 0
    assert (tied / "dispatch.json").read_bytes() == frozen_dispatch
    tied, home, children = _assembly_evidence(second, prepared_run=tied, active_limit=4)
    assert _assemble(tied, home).returncode == 0
    _record_native_schedule(tied, home, children, scenario="reversed-largest-order")
    reversed_ties = _assemble(tied, home)
    assert reversed_ties.returncode != 0
    assert "review-inspection-dispatch-order-mismatch" in reversed_ties.stderr


@pytest.mark.parametrize("peak", [4, 5])
def test_five_role_native_admission_limits_active_children(tmp_path: Path, peak: int) -> None:
    """Accept five completed roles in a four-child pool, rejecting five overlapping tasks."""
    run = _five_role_review_inputs(tmp_path)
    completed = _prepare(run)
    assert completed.returncode == 0, completed.stderr
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=peak)
    assembled = _assemble(run, home)
    if peak == 5:
        assert assembled.returncode != 0
        assert "review-inspection-active-capacity-exceeded" in assembled.stderr
        assert not (run / "specialist-manifest.json").exists()
        return
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 8
    assert {item["role"] for item in manifest["passes"]} == set(children)
    assert len(children) == 5
    for item in manifest["passes"]:
        assert item["mode"] == "inspection"
        terminal = json.loads(children[item["role"]].read_text(encoding="utf-8").splitlines()[-1])["payload"][
            "last_agent_message"
        ]
        assert (run / item["output_path"]).read_bytes() == (terminal.strip() + "\n").encode()


def test_default_historical_inspection_context_limit_remains_four(tmp_path: Path) -> None:
    """The explicit native roster extension cannot relax the shared default historical boundary."""
    run = _five_role_review_inputs(tmp_path)
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location(
        "historical_context_boundary", PLUGIN_ROOT / "shared/parallel_execution.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="review-inspection-contexts-invalid"):
        module.validate_inspection_contexts(plan, run / "inspection-plan.json")


@pytest.mark.parametrize("case", ["failed-peak-five", "missing-failed-timing", "sequential-selected"])
def test_native_retry_timing_cannot_evade_capacity_or_fabricate_parallelism(tmp_path: Path, case: str) -> None:
    """Count every launched attempt while deriving parallel review only from selected completions."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=4, final_header="missing")
    assembled = _assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    item = next(entry for entry in manifest["passes"] if entry["role"] == "sw-engineer")
    failed = {**item["attempts"][0], "status": "failed", "error_type": "transport_error"}
    selected = {
        **item["attempts"][0],
        "attempt": 2,
        "agent_thread_id": "child-retried",
        "agent_path": item["attempts"][0]["agent_path"].removesuffix("_a1") + "_a2",
        "turn_id": "turn-retried",
        "spawn_call_id": "spawn-retried",
    }
    item.update(attempts=[failed, selected], selected_attempt=2)
    old_rows = [json.loads(line) for line in children["sw-engineer"].read_text(encoding="utf-8").splitlines()]
    new_rows = json.loads(
        json.dumps(old_rows)
        .replace(failed["agent_thread_id"], selected["agent_thread_id"])
        .replace(failed["agent_path"], selected["agent_path"])
        .replace(failed["turn_id"], selected["turn_id"])
    )
    page = 0
    for row in new_rows:
        payload = row.get("payload", {})
        if (
            payload.get("type") == "agent_message"
            and payload.get("content", [{}])[0].get("type") == "encrypted_content"
        ):
            content = payload["content"][0]
            content["encrypted_content"] = content["encrypted_content"].replace("--attempt 1", "--attempt 2")
        elif payload.get("type") == "custom_tool_call":
            payload["input"] = payload["input"].replace("--attempt 1", "--attempt 2")
        elif payload.get("type") == "custom_tool_call_output":
            page += 1
            payload["output"][1]["text"] = _CONTEXT_READER(run / "inspection-plan.json", "sw-engineer", 2, page)
    selected_path = home / "sessions/rollout-child-retried.jsonl"
    _write_jsonl(selected_path, new_rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    original_call = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == failed["spawn_call_id"]
        and row["payload"].get("type") == "function_call"
    )
    call = json.loads(json.dumps(original_call))
    call["payload"]["call_id"] = selected["spawn_call_id"]
    arguments = json.loads(call["payload"]["arguments"])
    arguments["task_name"] = arguments["task_name"].removesuffix("_a1") + "_a2"
    arguments["message"] = arguments["message"].replace("--attempt 1", "--attempt 2")
    call["payload"]["arguments"] = json.dumps(arguments)
    original_receipt = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == failed["spawn_call_id"]
        and row["payload"].get("type") == "function_call_output"
    )
    receipt = json.loads(json.dumps(original_receipt).replace(failed["agent_path"], selected["agent_path"]))
    receipt["payload"]["call_id"] = selected["spawn_call_id"]
    original_join = next(row for row in parent if row.get("payload", {}).get("author") == failed["agent_path"])
    joined = json.loads(json.dumps(original_join).replace(failed["agent_path"], selected["agent_path"]))
    parent.extend([call, receipt, joined])
    _write_jsonl(parent_path, parent)
    for index, entry in enumerate(manifest["passes"]):
        attempt = entry["attempts"][entry["selected_attempt"] - 1]
        path = selected_path if entry["role"] == "sw-engineer" else children[entry["role"]]
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        completed = next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "task_complete")
        epoch = datetime(2026, 1, 1, 10, tzinfo=timezone.utc).timestamp()
        selected_starts = {"qa-specialist": 1, "doc-scribe": 3, "data-steward": 5, "challenger": 7, "sw-engineer": 9}
        start = (
            epoch + selected_starts[entry["role"]]
            if case == "sequential-selected"
            else epoch + (100 + index if index < 4 else 130)
        )
        completed.update(started_at=start, completed_at=start + (1 if case == "sequential-selected" else 10))
        assert completed["turn_id"] == attempt["turn_id"]
        _write_jsonl(path, rows)
    failed_rows = [json.loads(line) for line in children["sw-engineer"].read_text(encoding="utf-8").splitlines()]
    failed_completion = next(
        row["payload"] for row in failed_rows if row.get("payload", {}).get("type") == "task_complete"
    )
    failed_completion.update(
        started_at=epoch + (0.2 if case == "sequential-selected" else 105),
        completed_at=epoch + (1.5 if case == "sequential-selected" else 110),
    )
    if case == "missing-failed-timing":
        failed_rows = [row for row in failed_rows if row.get("payload", {}).get("type") != "task_complete"]
    _write_jsonl(children["sw-engineer"], failed_rows)
    if case != "missing-failed-timing":
        # Keep every launch and join coherent with its selected or failed task.
        # The negative retry overlaps four live children; the sequential selected
        # case instead refills joined slots while failed work alone overlaps.
        launch_offsets = {
            "qa-specialist": 0.28,
            "doc-scribe": 0.38,
            "data-steward": 0.48,
            "challenger": 1.8 if case == "sequential-selected" else 5.28,
            "sw-engineer": 4.2 if case == "sequential-selected" else 5,
        }
        path_times = {}
        for role, path in {**children, "sw-engineer": selected_path}.items():
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            agent_path = rows[0]["payload"]["agent_path"]
            launch = epoch + launch_offsets[role]
            terminal = next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "task_complete")
            end = terminal["completed_at"] + 0.05
            rows[0]["payload"]["timestamp"] = (
                datetime.fromtimestamp(launch + 0.005, timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            _write_jsonl(path, rows)
            path_times[agent_path] = (launch, end)
        path_times[failed["agent_path"]] = (epoch + 0.18, failed_completion["completed_at"] + 0.05)
        parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
        call_times = {}
        for row in parent:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                arguments = json.loads(payload["arguments"])
                path = next(path for path in path_times if path.rsplit("/", 1)[-1] == arguments["task_name"])
                value = path_times[path][0]
                call_times[payload["call_id"]] = value
            elif payload.get("type") == "function_call_output":
                value = call_times[payload["call_id"]] + 0.01
            elif payload.get("type") == "agent_message":
                value = path_times[payload["author"]][1]
            else:
                continue
            row["timestamp"] = (
                datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
        parent.sort(key=lambda row: row.get("timestamp", ""))
        _write_jsonl(parent_path, parent)
    spec = importlib.util.spec_from_file_location("retry_capacity_validator", SKILL / "validate_artifacts.py")
    validator = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = validator
    spec.loader.exec_module(validator)
    if case != "sequential-selected":
        expected = (
            "review-inspection-active-capacity-exceeded"
            if case == "failed-peak-five"
            else "review-inspection-attempt-timing-missing:sw-engineer:1"
        )
        with pytest.raises(SystemExit, match=expected):
            validator._validate_manifest_entries(
                run,
                manifest,
                manifest["passes"],
                set(children),
                home,
                "parent",
                tmp_path,
                require_role_card_receipts=True,
            )
    else:
        validator._validate_manifest_entries(
            run, manifest, manifest["passes"], set(children), home, "parent", tmp_path, require_role_card_receipts=True
        )
        summary = validator._validate_review_runtime(run, manifest, manifest["passes"], home, "parent")
        assert summary["actual_mode"] == "independent-spawned"


@pytest.mark.parametrize(
    "scenario",
    [
        "proper-refill",
        "blocked-wait-coalesces-joins",
        "wait-for-all-before-refill",
        "spawn-all-five",
        "reversed-largest-order",
        "wait-again-with-free-slot",
        "default-wait-again-with-free-slot",
        "30s-wait-again-with-free-slot",
        "completed-wait-again-with-free-slot",
        "join-before-terminal",
    ],
)
def test_actual_native_parent_sequence_controls_allocation_order_and_refill(tmp_path: Path, scenario: str) -> None:
    """Reject invalid parent scheduling even when every role completes and work peak remains four."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=4)
    # Retain a valid manifest first so both assembly and ordinary preflight can replay changed parent history.
    initial = _assemble(run, home)
    assert initial.returncode == 0, initial.stderr
    _record_native_schedule(run, home, children, scenario=scenario)
    assembled = _assemble(run, home)
    checked = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
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
    if scenario in {"proper-refill", "blocked-wait-coalesces-joins", "wait-for-all-before-refill"}:
        assert assembled.returncode == checked.returncode == 0, assembled.stderr + checked.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
        assert {item["role"] for item in manifest["passes"]} == set(children)
    else:
        assert assembled.returncode != 0, f"invalid {scenario} scheduling admitted"
        assert checked.returncode != 0, f"ordinary preflight admitted {scenario}"
        expected = {
            "spawn-all-five": "review-inspection-active-capacity-exceeded",
            "reversed-largest-order": "review-inspection-dispatch-order-mismatch",
            "wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
            "default-wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
            "30s-wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
            "completed-wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
            "join-before-terminal": "review-inspection-active-capacity-exceeded",
        }[scenario]
        assert expected in assembled.stderr
        assert expected in checked.stderr


@pytest.mark.parametrize("pool_size", [1, 2, 3])
def test_smaller_observed_native_pool_refills_complete_roster(tmp_path: Path, pool_size: int) -> None:
    """Accept a coherent smaller active pool without dropping roles or inventing available capacity."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=pool_size)
    _record_native_schedule(
        run,
        home,
        children,
        pool_size=pool_size,
        scenario="capacity-rejected-repeat" if pool_size == 1 else "capacity-rejected",
    )
    result = _assemble(run, home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    assert {item["role"] for item in manifest["passes"]} == set(children)
    assert len(manifest["passes"]) == 5
    summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    assert summary["actual_mode"] == ("independent-spawned" if pool_size == 1 else "parallel")
    assert summary["capacity_limited"] is True
    checked = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
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
    assert checked.returncode == 0, checked.stderr


def test_serial_native_roster_without_capacity_refusal_is_not_parallel_evidence(tmp_path: Path) -> None:
    """Reject arbitrary serialization while preserving full reviewer coverage."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, _ = _assembly_evidence(tmp_path, prepared_run=run, active_limit=1)
    result = _assemble(run, home)
    assert result.returncode != 0
    assert "review-wave-not-parallel" in result.stderr


@pytest.mark.parametrize(
    ("retry_offset", "state_change"),
    [
        pytest.param(0.2, False, id="without-release"),
        pytest.param(4.2, True, id="after-two-releases"),
        pytest.param(0.0, False, id="same-start"),
        pytest.param(0.005, False, id="overlapping-output"),
    ],
)
def test_same_queued_role_capacity_refusal_requires_each_verified_release(
    tmp_path: Path, retry_offset: float, state_change: bool
) -> None:
    """Require a genuine slot release before every same-task refusal retry."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=4)
    if not state_change:
        initial = _assemble(run, home)
        assert initial.returncode == 0, initial.stderr
    _record_native_schedule(run, home, children, scenario="capacity-rejected")
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    refusal_output = next(
        row
        for row in parent
        if row.get("payload", {}).get("output") == "collab spawn failed: agent thread limit reached"
    )
    refusal_id = refusal_output["payload"]["call_id"]
    refusal_call = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == refusal_id and row["payload"]["type"] == "function_call"
    )
    second_call = json.loads(json.dumps(refusal_call))
    second_output = json.loads(json.dumps(refusal_output))
    epoch = datetime(2026, 1, 1, 10, tzinfo=timezone.utc).timestamp()
    second_at = epoch + 1 + retry_offset
    for row, instant in [(second_call, second_at), (second_output, second_at + 0.01)]:
        row["payload"]["call_id"] = refusal_id + "-retry"
        row["timestamp"] = (
            datetime.fromtimestamp(instant, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
    parent.extend([second_call, second_output])
    arguments = json.loads(refusal_call["payload"]["arguments"])
    role = arguments["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
    if state_change:
        # First refusal precedes the first join; the second refusal follows it.
        # Only the next joined child frees capacity for the successful same task.
        child_rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        agent_path = child_rows[0]["payload"]["agent_path"]
        child_rows[0]["payload"]["timestamp"] = "2026-01-01T10:00:10.285Z"
        terminal = next(row["payload"] for row in child_rows if row.get("payload", {}).get("type") == "task_complete")
        terminal.update(started_at=epoch + 10.3, completed_at=epoch + 20.3)
        _write_jsonl(children[role], child_rows)
        successful = next(
            row
            for row in parent
            if row.get("payload", {}).get("type") == "function_call"
            and row["payload"].get("name") == "spawn_agent"
            and row["payload"].get("call_id") not in {refusal_id, refusal_id + "-retry"}
            and json.loads(row["payload"]["arguments"])["task_name"] == arguments["task_name"]
        )
        successful["timestamp"] = "2026-01-01T10:00:10.280Z"
        success_id = successful["payload"]["call_id"]
        for row in parent:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call_output" and payload.get("call_id") == success_id:
                row["timestamp"] = "2026-01-01T10:00:10.290Z"
            elif payload.get("type") == "agent_message" and payload.get("author") == agent_path:
                row["timestamp"] = "2026-01-01T10:00:20.350Z"
    parent.sort(key=lambda row: row.get("timestamp", ""))
    _write_jsonl(parent_path, parent)
    assembled = _assemble(run, home)
    checked = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
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
    if state_change:
        assert assembled.returncode == checked.returncode == 0, assembled.stderr + checked.stderr
        summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
        assert summary["capacity_limited"] is True
        manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
        assert {item["role"] for item in manifest["passes"]} == set(children)
    else:
        expected = f"review-inspection-capacity-refusal-no-state-change:{role}"
        assert assembled.returncode != 0
        assert checked.returncode != 0
        assert expected in assembled.stderr
        assert expected in checked.stderr
