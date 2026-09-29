"""Exercise deterministic review preparation without model-authored provenance."""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills/code-review"
HELPER = SKILL / "review_prepare.py"


def _review_inputs(
    tmp_path: Path,
    *,
    untracked: bool = False,
    second_file: bool = False,
    unchanged_caller: bool = False,
    deleted: bool = False,
) -> Path:
    """Write semantic reviewer decisions while leaving mechanical evidence to the producer."""
    spec = importlib.util.spec_from_file_location("prepare_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = tmp_path / "review"
    run.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    for command in (["init", "-q"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.com"]):
        subprocess.run(["git", "-C", str(repository), *command], check=True, capture_output=True)
    (repository / "widget.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "widget.py"], check=True, capture_output=True)
    if second_file:
        (repository / "other.py").write_text("other = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "other.py"], check=True, capture_output=True)
    if unchanged_caller:
        (repository / "stable.py").write_text("caller = 5\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "stable.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "Initial"], check=True, capture_output=True)
    (repository / "widget.py").write_text("value = 2\n", encoding="utf-8")
    if second_file:
        (repository / "other.py").write_text("other = 2\n", encoding="utf-8")
    if deleted:
        (repository / "widget.py").unlink()
    if untracked:
        (repository / "new.txt").write_text("new_value = 7\n", encoding="utf-8")
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
        check=False,
    )
    assert collected.returncode == 0, collected.stderr
    (run / "diff.patch").write_bytes((run / "local-source/diff.patch").read_bytes())
    if untracked:
        (run / "untracked.txt").write_text("new.txt\n", encoding="utf-8")
    roles = ["challenger", "qa-specialist"]
    routing = {
        "schema_version": 1,
        "risk_tier": "HIGH_RISK",
        "signals": dict.fromkeys(module.ROUTING_SIGNALS, False),
        "signal_evidence": {name: ["Scope checked."] for name in module.ROUTING_SIGNALS},
        "triggered_roles": roles,
        "trigger_reasons": {role: ["High-risk behavior."] for role in roles},
    }
    (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8")
    briefs = {}
    for role in roles:
        (run / f"{role}-evidence.md").write_text(
            f"Scope: widget.py at frozen revision. Excluded: unrelated callers.\nQuestion: {role} axis.\n"
            + "Relevant source evidence.\n" * 300,
            encoding="utf-8",
        )
        briefs[role] = {"axis": role, "evidence_path": f"{role}-evidence.md", "source_paths": ["widget.py"]}
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
    return run


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
        check=False,
    )


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
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
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
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
    if tampered:
        (root / "stable.py").write_text("caller = 999\n", encoding="utf-8")
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
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
    result = _prepare(run)
    assert result.returncode == 2
    assert "review-brief-source-selection-invalid:challenger" in result.stderr
    assert not (run / "dispatch.json").exists()


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """Write synthetic native rollout rows with portable line endings."""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def _assembly_evidence(tmp_path: Path, *, malformed_continuation: bool = False) -> tuple[Path, Path, dict[str, Path]]:
    """Record dispatched calls, real context-reader output, and completed child turns."""
    run = _review_inputs(tmp_path)
    prepared = _prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    if malformed_continuation:
        message = dispatch["calls"][0]["arguments"]["message"]
        dispatch["calls"][0]["arguments"]["message"] = message.replace(" --page N", " --page WRONG")
    home = tmp_path / "codex-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    parent_rows: list[dict[str, object]] = [{"type": "session_meta", "payload": {"id": "parent"}}]
    children = {}
    for index, (context, call) in enumerate(zip(plan["contexts"], dispatch["calls"], strict=True), start=1):
        role = context["role_id"]
        arguments = call["arguments"]
        agent_path = f"/root/{arguments['task_name']}"
        thread = f"child-{index}"
        turn = f"turn-{index}"
        call_id = f"spawn-{index}"
        message = arguments["message"]
        page_match = re.search(r"Read all (\d+) frozen review context pages", message)
        assert page_match is not None, "compact-review-page-count-invalid"
        page_count = int(page_match.group(1))
        assert page_count > 1
        instruction = (
            f"For pages 2 through {page_count}, copy the same JavaScript source once per page in order. "
            "In each copied source, append ` --page N` to the end of the `cmd` string, replacing N with that page's "
            "actual number. Keep every other byte of the JavaScript source unchanged."
        )
        assert instruction in message, "compact-review-continuation-invalid"
        templates = re.findall(r"```javascript\n(.*?)\n```", message, flags=re.DOTALL)
        assert len(templates) == 1, "compact-review-template-count-invalid"
        first_call = templates[0]
        prefix, command_tail = first_call.split("tools.exec_command(", 1)
        args_json, suffix = command_tail.split("); text(r.output);", 1)
        command_args = json.loads(args_json)
        assert command_args["cmd"].endswith("--attempt 1"), "compact-review-template-command-invalid"
        read_calls = [first_call]
        for page in range(2, page_count + 1):
            paged_args = {**command_args, "cmd": f"{command_args['cmd']} --page {page}"}
            read_calls.append(
                f"{prefix}tools.exec_command({json.dumps(paged_args, ensure_ascii=False)}); text(r.output);{suffix}"
            )
        tool_rows = []
        header = ""
        for page, read_call in enumerate(read_calls, start=1):
            reader = subprocess.run(
                [
                    sys.executable,
                    str(SKILL / "review_context.py"),
                    "--plan",
                    str(run / "inspection-plan.json"),
                    "--role",
                    role,
                    "--attempt",
                    "1",
                    "--page",
                    str(page),
                ],
                capture_output=True,
                check=False,
            )
            assert reader.returncode == 0, reader.stderr
            output = reader.stdout.decode("utf-8")
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
        final = (
            f"{header}\nNo finding.\n\n## Reviewer Assessment\n\nRating: 1\nRationale: The inspected scope is clean."
        )
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
                        "started_at": 100 + index,
                        "completed_at": 110 + index,
                        "last_agent_message": final,
                    },
                },
            ],
        )
        children[role] = child
    _write_jsonl(sessions / "rollout-parent.jsonl", parent_rows)
    (run / "specialist-assessments.json").write_text(
        json.dumps({role: {"confidence": 0.95, "blocking_findings": 0} for role in children}), encoding="utf-8"
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


def test_prepare_freezes_complete_wave_and_keeps_source_out_of_dispatch(tmp_path: Path) -> None:
    run = _review_inputs(tmp_path)
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    assert dispatch["routing_sha256"] == hashlib.sha256((run / "review-routing.json").read_bytes()).hexdigest()
    assert dispatch["briefs_sha256"] == hashlib.sha256((run / "review-briefs.json").read_bytes()).hexdigest()
    assert [entry["role_id"] for entry in plan["contexts"]] == ["challenger", "qa-specialist"]
    assert len(dispatch["calls"]) == 2
    for entry, call in zip(plan["contexts"], dispatch["calls"], strict=True):
        context = (run / entry["context_path"]).read_bytes()
        role = entry["role_id"]
        assert context.startswith((PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes())
        assert "Relevant source evidence." not in call["arguments"]["message"]
        assert call["arguments"]["fork_turns"] == "none"
        assert call["arguments"]["agent_type"] == "default"
        assert entry["context_sha256"][:12] in call["arguments"]["task_name"]
        assert len(re.findall(r"```javascript\n", call["arguments"]["message"])) == 1
        assert "append ` --page N`" in call["arguments"]["message"]
    assert dispatch["context_bytes"] > dispatch["dispatch_bytes"]


def test_prepare_rejects_missing_role_before_freezing_any_context(tmp_path: Path) -> None:
    run = _review_inputs(tmp_path)
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs.pop("challenger")
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
    result = _prepare(run)
    assert result.returncode != 0
    assert "review-brief-role-set-mismatch" in result.stderr
    assert not (run / "inspection-plan.json").exists()


def test_prepare_rejects_prose_only_source_claim_before_freezing(tmp_path: Path) -> None:
    """A claimed inspection cannot substitute for source bytes from the collected checkout."""
    run = _review_inputs(tmp_path)
    (run / "challenger-evidence.md").write_text("I inspected widget.py at the frozen revision.\n", encoding="utf-8")
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"].pop("source_paths")
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
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
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
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
    source.write_text("value = 3\n", encoding="utf-8")
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
    (review / "widget.py").write_text("value = 1\n", encoding="utf-8")
    original = "old.py" if rename else destination
    (review / original).write_text("def renamed():\n    return 17\n", encoding="utf-8")
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
        (review / destination).write_text("def renamed():\n    return 18\n", encoding="utf-8")
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
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")

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
    (review / "widget.py").write_text("value = 3\n", encoding="utf-8")
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
    (run / "pr/local-checkout.json").write_text(json.dumps(receipt), encoding="utf-8")
    result = _prepare(run, source_root=review)
    assert result.returncode == 0, result.stderr
    assert "value = 3\n" in (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    (run / "local-checkout.json").write_text(json.dumps(receipt), encoding="utf-8")
    result = _prepare(run, source_root=review)
    assert result.returncode != 0
    assert "review-source-receipt-ambiguous" in result.stderr


def test_prepare_rejects_source_drift_before_freezing(tmp_path: Path) -> None:
    """A modified isolated checkout invalidates the retained source snapshot."""
    run = _review_inputs(tmp_path)
    review = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    )
    (review / "widget.py").write_text("value = 99\n", encoding="utf-8")
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
    (repository / "unchanged.py").write_text("stable = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "unchanged.py"], check=True, capture_output=True)
    # Selection outside the collector's frozen source inventory must fail closed.
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"] = ["unchanged.py"]
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
    result = _prepare(run)
    assert result.returncode != 0
    assert (
        "review-brief-source-unrelated:challenger" in result.stderr
        or "review-brief-source-selection-invalid:challenger" in result.stderr
    )
    assert not (run / "inspection-plan.json").exists()


def test_prepare_rejects_oversized_context_before_freezing_wave(tmp_path: Path) -> None:
    """Keep a reviewer brief above the native read limit out of the frozen wave."""
    run = _review_inputs(tmp_path)
    (run / "challenger-evidence.md").write_text("bounded evidence\n" * 5000, encoding="utf-8")
    result = _prepare(run)
    assert result.returncode != 0
    assert "review-context-capacity-exceeded:challenger:65536-bytes" in result.stderr
    assert not (run / "inspection-plan.json").exists()
    assert not (run / "specialists").exists()


@pytest.mark.parametrize("problem", ["changed-brief", "sensitive-evidence"])
def test_prepare_never_overwrites_frozen_evidence_or_retains_secrets(tmp_path: Path, problem: str) -> None:
    run = _review_inputs(tmp_path)
    if problem == "changed-brief":
        assert _prepare(run).returncode == 0
        original = (run / "inspection-plan.json").read_bytes()
        (run / "qa-specialist-evidence.md").write_text("Different source.")
        expected = "review-frozen-artifact-conflict"
    else:
        (run / "qa-specialist-evidence.md").write_text("Authorization: Bearer do-not-retain")
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
    assert manifest["schema_version"] == 6
    assert summary["actual_mode"] == "parallel"
    assert {item["role"] for item in manifest["passes"]} == set(children)
    for item in manifest["passes"]:
        role = item["role"]
        rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        final = rows[-1]["payload"]["last_agent_message"]
        assert (run / item["output_path"]).read_text(encoding="utf-8") == final + "\n"
        assert item["attempts"][0]["agent_thread_id"] == rows[0]["payload"]["id"]


def test_assembly_fixture_rejects_malformed_compact_continuation(tmp_path: Path) -> None:
    """Make synthetic child calls depend on the actual compact dispatch instruction."""
    with pytest.raises(AssertionError, match="compact-review-continuation-invalid"):
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
    )
    with pytest.raises(SystemExit, match="review-assessment-content-invalid:challenger"):
        module._retained_reviewer_rating(response, app_server=False, main=False, role="challenger")


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
        rows[-1]["payload"].update(started_at=200, completed_at=210)
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
    assert actual.endswith(expected_body)
    assert b"\r\r\n" not in actual
    assert f"codex-review-context-page {page}/2".encode() in actual
