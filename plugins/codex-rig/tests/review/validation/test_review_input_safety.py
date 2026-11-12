"""Reject malformed review evidence without weakening historical or valid input handling."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from _modules import load_module
from _platform import FILE_SYMLINKS_AVAILABLE

SKILL = Path(__file__).resolve().parents[3] / "skills/code-review"


def _dispatch_plan(tmp_path: Path, coordinate: str) -> Path:
    """Create a complete renderable plan with an explicitly selected context coordinate."""
    run = tmp_path / "run"
    run.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("Frozen review evidence", encoding="utf-8")
    context_path = {"relative": "../outside.md", "absolute": str(outside), "symlink": "context.md"}[coordinate]
    if coordinate == "symlink":
        (run / context_path).symlink_to(outside)
    plan = run / "inspection-plan.json"
    plan.write_text(
        json.dumps({"contexts": [{"role_id": "reviewer", "context_path": context_path, "context_sha256": "a" * 64}]}),
        encoding="utf-8",
    )
    return plan


@pytest.mark.parametrize(
    "coordinate",
    [
        "relative",
        "absolute",
        pytest.param(
            "symlink", marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="file symlinks unavailable")
        ),
    ],
)
def test_prepare_rejects_context_escape_before_rendering(
    tmp_path: Path, coordinate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan cannot make dispatch preparation read evidence outside its own directory."""
    prepare = load_module("review_dispatch_bound_prepare", SKILL / "review_prepare.py")
    plan = _dispatch_plan(tmp_path, coordinate)
    reader = Mock(wraps=prepare.review_context.dispatch_message)
    monkeypatch.setattr(prepare.review_context, "dispatch_message", reader)
    with pytest.raises(ValueError, match="review-context-outside-plan"):
        prepare._dispatch_message(plan, "reviewer")
    reader.assert_not_called()


@pytest.mark.parametrize(
    "coordinate",
    [
        "relative",
        "absolute",
        pytest.param(
            "symlink", marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="file symlinks unavailable")
        ),
    ],
)
def test_native_validation_rejects_context_escape_before_rendering(
    validator: ModuleType, tmp_path: Path, coordinate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native recipe reconstruction rejects escaped contexts before invoking the historical reader."""
    plan = _dispatch_plan(tmp_path, coordinate)
    globals_ = validator._native_dispatch_message.__globals__
    reader = Mock(wraps=globals_["dispatch_message"])
    monkeypatch.setitem(globals_, "dispatch_message", reader)
    with pytest.raises(SystemExit, match="review-context-outside-plan"):
        validator._native_dispatch_message(
            {"schema_version": 6, "context_reader_python": sys.executable}, plan, "reviewer", 1
        )
    reader.assert_not_called()


def test_dispatch_callers_preserve_contained_context_recipes(validator: ModuleType, tmp_path: Path) -> None:
    """Contained context plans retain the exact compact and historical dispatch recipes."""
    prepare = load_module("review_dispatch_valid_prepare", SKILL / "review_prepare.py")
    context = tmp_path / "context.md"
    context.write_text("Frozen review evidence", encoding="utf-8")
    plan = tmp_path / "inspection-plan.json"
    plan.write_text(
        json.dumps({"contexts": [{"role_id": "reviewer", "context_path": context.name, "context_sha256": "a" * 64}]}),
        encoding="utf-8",
    )
    message = prepare.review_context.dispatch_message(plan, "reviewer", provenance_header=False)
    assert prepare._dispatch_message(plan, "reviewer") == prepare._compact_dispatch_message(message, "a" * 64)
    historical = prepare.review_context.dispatch_message(plan, "reviewer", _all_page_calls=False, _include_workdir=True)
    assert (
        validator._native_dispatch_message(
            {"schema_version": 6, "context_reader_python": sys.executable}, plan, "reviewer", 1
        )
        == historical
    )


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="file symlinks unavailable")
def test_native_validation_checks_selected_historical_plan_directory(validator: ModuleType, tmp_path: Path) -> None:
    """A valid retained context cannot authorize an escaped context at the original recipe coordinate."""
    original = _dispatch_plan(tmp_path, "symlink")
    retained = tmp_path / "retained"
    retained.mkdir()
    plan = retained / original.name
    plan.write_bytes(original.read_bytes())
    (retained / "context.md").write_text("Frozen review evidence", encoding="utf-8")
    manifest = {
        "schema_version": 8,
        "dispatch_protocol": "paged-context-v6",
        "original_plan_path": str(original),
        "context_reader_python": sys.executable,
        "context_reader_path": str(SKILL / "review_context.py"),
    }
    with pytest.raises(SystemExit, match="review-context-outside-plan"):
        validator._native_dispatch_message(manifest, plan, "reviewer", 1)


@pytest.fixture
def validator() -> ModuleType:
    """Load the ordinary artifact validator and its execution helpers."""
    return load_module("review_input_safety_validator", SKILL / "validate_artifacts.py")


@pytest.mark.parametrize("schema", [None, 1, 2])
def test_fresh_candidate_requires_current_schema(validator: ModuleType, tmp_path: Path, schema: int | None) -> None:
    """A fresh result cannot use historical schemas to bypass current evidence checks."""
    result = {"metadata": {"finding_records_version": 2}, **({"schema_version": schema} if schema is not None else {})}
    context = {
        "result": result,
        "metadata": result["metadata"],
        "scope": "path",
        "risk_tier": "LOCAL",
        "status": "pass",
    }
    steps = validator._assessed_review_steps(tmp_path, tmp_path / "candidate.json", context, None)
    decision = next(step for step in steps if step.name == "review-decision")
    with pytest.raises(SystemExit, match="review-current-validation-required-rerun-code-review"):
        decision.check()


def test_notes_require_standalone_headings(validator: ModuleType, tmp_path: Path) -> None:
    """Prose that mentions every section cannot masquerade as a complete report."""
    notes = tmp_path / "review-notes.md"
    notes.write_text(
        "See " + " and ".join("## " + section for section in validator.REQUIRED_SECTIONS), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="missing-review-note-sections"):
        validator._require_notes_sections(notes)


@pytest.mark.parametrize("suffix", ["", " ##"])
def test_notes_accept_real_headings(validator: ModuleType, tmp_path: Path, suffix: str) -> None:
    """Complete reports retain support for level-one and level-two headings."""
    notes = tmp_path / "review-notes.md"
    notes.write_text("\n".join("## " + section + suffix for section in validator.REQUIRED_SECTIONS), encoding="utf-8")
    validator._require_notes_sections(notes)


def test_session_index_retains_large_valid_headers(validator: ModuleType, tmp_path: Path) -> None:
    """Base instructions can exceed short line limits without losing required child identity."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "child.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "child", "base_instructions": "x" * 100000}}),
        encoding="utf-8",
    )
    indexed = validator._session_metadata(tmp_path)
    assert len(indexed) == 1
    assert indexed[0]["id"] == "child"
    assert "base_instructions" not in indexed[0]


def _write_session_history(sessions: Path, count: int) -> None:
    """Create unrelated one-line logs without relying on a repository or external sessions."""
    sessions.mkdir()
    for index in range(count):
        (sessions / f"unrelated-{index}.jsonl").write_bytes(b'{"type":"event_msg"}\n')


def test_session_index_accepts_large_unrelated_history(validator: ModuleType, tmp_path: Path) -> None:
    """Unrelated session history cannot impose a global provenance admission limit."""
    _write_session_history(tmp_path / "sessions", 10001)
    (tmp_path / "sessions/child.jsonl").write_bytes(b'{"type":"session_meta","payload":{"id":"child"}}\n')
    assert [session["id"] for session in validator._session_metadata(tmp_path)] == ["child"]


def test_session_readers_decode_only_header_line(validator: ModuleType, tmp_path: Path) -> None:
    """Invalid later bytes cannot hide a child whose metadata header is valid UTF-8."""
    prepare = load_module("review_header_prepare", SKILL / "review_prepare.py")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    child = sessions / "child.jsonl"
    header = {
        "type": "session_meta",
        "payload": {
            "id": "child",
            "agent_path": "/root/reviewer",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
        },
    }
    child.write_bytes(json.dumps(header).encode("utf-8") + b"\n\xff\n")
    indexed = validator._session_metadata(tmp_path)
    children = prepare._child_sessions(tmp_path, "parent")
    assert ([session["id"] for session in indexed], children) == (["child"], {"reviewer": [child]})


def test_session_readers_record_unreadable_files(
    validator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked session remains unknown instead of disappearing from absence checks."""
    prepare = load_module("review_unreadable_prepare", SKILL / "review_prepare.py")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    unknown = sessions / "unknown.jsonl"
    unknown.write_bytes(b"unavailable")
    monkeypatch.setattr(Path, "open", Mock(spec=Path.open, side_effect=PermissionError("locked session")))

    assert validator._session_metadata(tmp_path) == [{"error": "provenance-session-unreadable", "path": str(unknown)}]
    assert prepare._child_sessions(tmp_path, "parent") == {"provenance-session-unreadable": [unknown]}


def test_child_index_skips_parseable_non_session_files(tmp_path: Path) -> None:
    """Ordinary JSONL event files do not become unknown session metadata."""
    prepare = load_module("review_non_session_prepare", SKILL / "review_prepare.py")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "event.jsonl").write_bytes(b'{"type":"event_msg"}\n')
    assert prepare._child_sessions(tmp_path, "parent") == {}


@pytest.mark.parametrize("payload", ['{"status": []}', pytest.param("[" * 2000 + "]" * 2000, id="deep-json")])
def test_all_errors_returns_json_for_malformed_result(tmp_path: Path, payload: str) -> None:
    """The all-errors CLI emits a structured failure instead of a traceback for hostile JSON shapes."""
    candidate = tmp_path / "candidate.json"
    candidate.write_text(payload, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
            "--out",
            str(tmp_path),
            "--result",
            str(candidate),
            "--all-errors",
            "--parent-thread-id",
            "parent",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "fail"
    assert report["errors"][0]["step"] == "result-shape"
    assert "Traceback" not in result.stderr


def test_fragments_reject_invalid_utf8_without_underflow() -> None:
    """Continuation-only input fails explicitly rather than underflowing or making no progress."""
    batches = load_module("review_input_safety_batches", SKILL / "review_batches.py")
    with pytest.raises(ValueError, match="review-interaction-fragment"):
        batches._bounded_fragments(b"\x80" * 20000, 16000, "module.py", 0)


@pytest.mark.parametrize("directory", ["../elsewhere", "batches/interaction-002", "batches/../elsewhere"])
def test_interaction_schedule_rejects_noncanonical_paths(tmp_path: Path, directory: str) -> None:
    """Consolidation reads only the canonical contained interaction wave for each schedule entry."""
    batches = load_module("review_input_safety_paths", SKILL / "review_batches.py")
    (tmp_path / "interaction-dispatch.json").write_text(
        json.dumps({"schema_version": 1, "waves": [{"directory": directory}]}), encoding="utf-8"
    )
    with pytest.raises((ValueError, SystemExit), match=r"interaction|outside"):
        batches._interaction_wave_paths(tmp_path)


@pytest.mark.parametrize("stray", [b'{"type":"event_msg"}\n', b"\xff\xfe\n"])
def test_receipt_ignores_unrelated_non_session_logs(validator: ModuleType, tmp_path: Path, stray: bytes) -> None:
    """A stray JSONL file cannot invalidate otherwise unique child lineage."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "stray.jsonl").write_bytes(stray)
    (sessions / "child.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "child",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "parent_thread_id": "parent",
                    "agent_path": "/parent/reviewer",
                    "source": {
                        "subagent": {"thread_spawn": {"parent_thread_id": "parent", "agent_path": "/parent/reviewer"}}
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    call = {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": {
            "type": "function_call",
            "call_id": "spawn",
            "name": "spawn_agent",
            "arguments": json.dumps({"task_name": "reviewer", "fork_turns": "none", "message": "context"}),
        },
    }
    receipt = {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:02Z",
        "payload": {
            "type": "function_call_output",
            "call_id": "spawn",
            "output": json.dumps({"task_name": "/parent/reviewer"}),
        },
    }
    attempt = {"spawn_call_id": "spawn", "agent_path": "/parent/reviewer", "agent_thread_id": "child"}
    assert validator._receipt_binds_child([call, receipt], tmp_path, "parent", attempt, "context")


@pytest.mark.parametrize("thread_id", ["*", "[A]AAA", "../thread"])
def test_rollout_lookup_rejects_glob_and_path_ids(validator: ModuleType, tmp_path: Path, thread_id: str) -> None:
    """Thread identifiers cannot become glob syntax or select an unrelated rollout."""
    with pytest.raises(SystemExit, match="provenance-thread-id-invalid"):
        validator._find_rollout(tmp_path, thread_id)


@pytest.mark.parametrize(
    ("executable", "filename"),
    [
        pytest.param("/opt/bin/python3", "/work/reader.py", id="posix"),
        pytest.param("/opt/tools:local/python3", "/work/reader.py", id="posix-colon"),
        pytest.param("C:\\bin\\python.exe", "C:\\work\\reader.py", id="windows"),
    ],
)
def test_missing_reader_diagnostic_preserves_host_coordinates(
    validator: ModuleType, executable: str, filename: str
) -> None:
    """A bounded diagnostic parser still recognizes both host path styles."""
    quoted = filename.replace("\\", "\\\\")
    diagnostic = f"{executable}: can't open file '{quoted}': [Errno 2] No such file or directory\n"
    assert validator._missing_reader_error_path(diagnostic) == filename


@pytest.mark.parametrize(
    ("operation", "content", "expected"),
    [
        pytest.param(
            "_missing_reader_error_path", "python: can't open file '" * 20000, "None", id="repeated-diagnostic"
        ),
        pytest.param("_reviewer_sections", "## " + " " * 100000, "[('', 0, 100003, 100003)]", id="blank-heading"),
        pytest.param(
            "_text_reviewer_assessment",
            "Rating: 2\nRationale: ok" + " " * 100000 + "end",
            "(2, 'ok",
            id="rationale-tail",
        ),
    ],
)
def test_reviewer_text_parsers_bound_adversarial_whitespace(
    tmp_path: Path, operation: str, content: str, expected: str
) -> None:
    """Untrusted final-answer text cannot stall validation through regex backtracking."""
    source = tmp_path / "response.txt"
    source.write_text(content, encoding="utf-8")
    code = (
        "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "import validate_artifacts; import review_execution_validation as v; "
        "f=getattr(v,sys.argv[2]); s=Path(sys.argv[3]).read_text(); "
        "r=f(s,batch_response=True) if sys.argv[2]=='_text_reviewer_assessment' else f(s); print(repr(r)[:100])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(SKILL), operation, str(source)],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith(expected)


@pytest.mark.parametrize("blank", ["##  ", "##\t\t"])
def test_blank_heading_invalidates_reviewer_section_inventory(validator: ModuleType, blank: str) -> None:
    """An extra empty-title section cannot become reviewer assessment rationale."""
    content = (
        "## Reviewer Findings\n```json\n[]\n```\n"
        "## Reviewer Confidence\n```json\n{}\n```\n"
        "## Reviewer Assessment\nRating: 2\nRationale: inspected source\n" + blank
    )
    with pytest.raises(SystemExit, match="review-batch-individual-findings-format"):
        validator._batch_response_parts(content, "reviewer")


@pytest.mark.parametrize(
    "path",
    [
        "src/oauth.py",
        "src/token.py",
        "src/secrets.py",
        "src/jwt.py",
        "src/crypto.py",
        "Dockerfile",
        ".github/actions/check/action.yml",
    ],
)
def test_security_and_build_names_raise_risk_floor(tmp_path: Path, path: str) -> None:
    """Common security and executable build paths cannot evade the mechanical high-risk floor."""
    routing = load_module("review_input_safety_routing", SKILL / "review_routing.py")
    (tmp_path / "files.txt").write_text(path + "\n", encoding="utf-8")
    tier, evidence, _ = routing.derive_mechanical_risk(tmp_path)
    assert tier == "HIGH_RISK"
    assert "high_risk_paths=" + path.lower() in evidence
