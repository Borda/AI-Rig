"""Guard challenge results against unverified clean and prior-finding claims."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from _artifact_files import read_jsonl, write_json
from test_loop_review_evidence import (
    REQUEST,
    _ledger_digest,
    _loop_evidence_run,
    _read_ledger,
    _repository,
    _rewrite_native_outputs,
    _validator,
    _write_ledger,
)


@pytest.fixture(autouse=True)
def _native_runtime_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the controlled native-review receipts to their parent thread."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")


@pytest.fixture
def current(tmp_path: Path) -> dict[str, object]:
    """Build a complete current-contract loop evidence run rooted at the test directory."""
    return _loop_evidence_run(tmp_path, current_contract=True)


def _path_of(run: dict[str, object], key: str) -> Path:
    """Return a path-valued entry of an evidence run, checking its type once."""
    value = run[key]
    assert isinstance(value, Path)
    return value


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows back as LF-terminated JSON lines."""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def _replace_author_message(rows: list[dict[str, Any]], author: str, message: str) -> None:
    """Replace the final-answer text of every parent rollout message written by one reviewer."""
    for row in rows:
        payload = row.get("payload", {})
        if payload.get("type") == "agent_message" and payload.get("author") == author:
            payload["content"][0]["text"] = f"Message Type: FINAL_ANSWER\nPayload:\n{message}"


def _bind_scope(current: dict[str, object], scope: str, reviewed_paths: list[str]) -> dict[str, object]:
    """Bind authentic-shaped review receipts to a fresh literal-scope snapshot and Git diff."""
    run = _path_of(current, "run")
    repository = _path_of(current, "repository")
    fixture = current["fixture"]
    validator = current["validator"]
    assert isinstance(fixture, dict)
    source = validator.capture_source_snapshot(repository, [scope])
    source_bytes = validator._canonical_source_bytes(source)
    for name in ("current-source.json", "source-1.json"):
        (run / name).write_bytes(source_bytes)
    diff = subprocess.run(
        ["git", "--literal-pathspecs", "diff", "HEAD", "--binary", "--no-renames", "--", scope],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    digest = hashlib.sha256(diff).hexdigest()
    (run / "round-1.diff").write_bytes(diff)
    (run / "review" / "diff.patch").write_bytes(diff)
    fixture["manifest"]["review_input_sha256"] = digest
    fixture["plan"]["review_input_sha256"] = digest
    ledger = _read_ledger(run / "loop-ledger.json")
    ledger["current_snapshot"]["diff_digest"] = digest
    ledger["rounds"][0]["snapshot"]["diff_digest"] = digest
    _write_ledger(run / "loop-ledger.json", ledger)
    evidence_path = run / "loop-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["scope_paths"] = [scope]
    write_json(evidence_path, evidence)
    _rewrite_native_outputs(
        fixture, source_bytes, diff, [], full=True, request=evidence["request"], coverage_paths=reviewed_paths
    )
    selected = fixture["manifest"]["passes"][0]["attempts"][0]
    (run / "review-1.md").write_bytes((run / "review" / selected["output_path"]).read_bytes())
    return source


def _leave_repository(repository: Path) -> None:
    """Leave the fixture repository untouched."""


def _ignore_scope_file(repository: Path) -> None:
    """Create `ignored.py` and exclude it through `.gitignore`."""
    (repository / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (repository / "ignored.py").write_text("VALUE = 1\n", encoding="utf-8")


def test_clean_ledger_rejects_reviewer_nonclean_source_rating(current: dict[str, object]) -> None:
    """A rating of four cannot certify a zero-finding challenge as clean.

    The reviewer's own output rates the source 4 while the ledger claims a clean zero-finding round; every receipt that
    quotes the output is rewritten consistently, so only the clean-versus-rating check can reject it.
    """
    run = _path_of(current, "run")
    fixture = current["fixture"]
    assert isinstance(fixture, dict)
    manifest = fixture["manifest"]
    attempt = manifest["passes"][0]["attempts"][0]
    output_path = run / "review" / attempt["output_path"]
    original = output_path.read_text(encoding="utf-8")
    assert '"rating": 1' in original or '"rating": 4' in original
    output = original.replace('"rating": 1', '"rating": 4')
    output_path.write_text(output, encoding="utf-8", newline="\n")
    attempt["output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    write_json(run / "review" / "specialist-manifest.json", manifest)
    (run / "review-1.md").write_bytes(output_path.read_bytes())
    message = output.removesuffix("\n")
    sessions = fixture["sessions"] / "sessions"
    child_path = sessions / f"rollout-{attempt['agent_thread_id']}.jsonl"
    child = read_jsonl(child_path)
    child[-1]["payload"]["last_agent_message"] = message
    _write_rows(child_path, child)
    parent_path = sessions / "rollout-parent-thread.jsonl"
    parent = read_jsonl(parent_path)
    _replace_author_message(parent, attempt["agent_path"], message)
    _write_rows(parent_path, parent)
    assert '"rating": 4' in output

    with pytest.raises(ValueError, match="loop-evidence-report-clean-rating-mismatch"):
        current["validator"].validate_loop_evidence(run, current["codex_home"])


def test_prior_finding_needs_its_own_raw_reviewer_verdict(tmp_path: Path) -> None:
    """Parent triage cannot turn an unrelated reviewed defect into closure of a prior one.

    The reviewer fixed a different defect; the parent claims it is the same as the earlier open `missing-guard` finding.
    Only a raw verdict by the reviewer on that exact signature may close it.
    """
    prior_finding = {
        "signature": "missing-guard",
        "tier": "high",
        "structural": False,
        "disposition": "open",
        "evidence": ["The widget lacks a guard."],
    }
    unrelated = {
        "signature": "different-bug",
        "tier": "high",
        "structural": False,
        "disposition": "verified-fixed",
        "evidence": ["A different bug was fixed."],
    }
    prior_root = tmp_path / "prior-root"
    current_root = tmp_path / "current-root"
    prior_root.mkdir()
    current_root.mkdir()
    prior = _loop_evidence_run(prior_root, findings=[prior_finding])
    current = _loop_evidence_run(current_root, findings=[unrelated], current_contract=True)
    run, prior_run, fixture = _path_of(current, "run"), _path_of(prior, "run"), current["fixture"]
    assert isinstance(fixture, dict)
    prior_block = "Prior findings to reassess:\n" + json.dumps([prior_finding], sort_keys=True, ensure_ascii=True)
    request = REQUEST | {"specification": REQUEST["specification"] + "\n" + prior_block}
    _rewrite_native_outputs(
        fixture,
        (run / "source-1.json").read_bytes(),
        (run / "round-1.diff").read_bytes(),
        [unrelated],
        full=True,
        request=request,
        coverage_paths=["widget.py"],
    )
    selected = fixture["manifest"]["passes"][0]["attempts"][0]
    (run / "review-1.md").write_bytes((run / "review" / selected["output_path"]).read_bytes())
    ledger = _read_ledger(run / "loop-ledger.json")
    ledger["rounds"][0]["findings"][0]["signature"] = "missing-guard"
    _write_ledger(run / "loop-ledger.json", ledger)
    evidence_path = run / "loop-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["request"] = request
    evidence["origin"] = {
        "kind": "continuation",
        "caller_run": prior_run.resolve().as_posix(),
        "prior_ledger_sha256": _ledger_digest(prior_run / "loop-ledger.json"),
    }
    evidence["rounds"][0]["triage"] = [
        {
            "reported_signature": "different-bug",
            "signature": "missing-guard",
            "tier": "high",
            "reason": "Claim these unrelated findings are the same.",
            "evidence": ["Parent asserts sameness."],
        }
    ]
    write_json(evidence_path, evidence)

    with pytest.raises(ValueError, match="loop-evidence-prior-raw-verdict-missing:missing-guard"):
        current["validator"].validate_loop_evidence(run, current["codex_home"])


class TestDirectoryScope:
    """A directory scope is judged by the concrete files the source snapshot captured."""

    @pytest.mark.parametrize(
        ("reviewed_paths", "outcome"),
        [
            pytest.param(
                ["."],
                pytest.raises(ValueError, match="loop-evidence-report-coverage-missing"),
                id="directory-only-coverage",
            ),
            pytest.param(["widget.py"], nullcontext(), id="each-captured-file"),
        ],
    )
    def test_requires_each_captured_file_in_coverage(
        self, current: dict[str, object], reviewed_paths: list[str], outcome: Any
    ) -> None:
        """Naming a directory must not conceal an omitted file, while naming each file needs no directory finding.

        The scope `.` captures `widget.py`; a reviewer that only claims `.` is rejected, and one that names the captured
        file is accepted without inventing a directory-level finding.
        """
        source = _bind_scope(current, ".", reviewed_paths)
        assert [record["path"] for record in source["files"]] == ["widget.py"]

        with outcome:
            current["validator"].validate_loop_evidence(current["run"], current["codex_home"])


class TestLiteralScope:
    """A literal primary scope must be represented by captured source before it can be reviewed."""

    @pytest.mark.parametrize(
        ("scope", "prepare"),
        [
            pytest.param("never-existed.py", _leave_repository, id="nonexistent"),
            pytest.param("ignored.py", _ignore_scope_file, id="ignored"),
        ],
    )
    def test_unrepresented_scope_cannot_be_reviewed(
        self, current: dict[str, object], scope: str, prepare: Callable[[Path], None]
    ) -> None:
        """A missing or ignored source file must not receive a fabricated clean review.

        Neither file reaches the source snapshot, so a clean review of it would certify nothing that was captured.
        """
        prepare(_path_of(current, "repository"))
        source = _bind_scope(current, scope, [scope])
        assert source["files"] == []

        with pytest.raises(ValueError, match="loop-evidence-primary-source-incomplete"):
            current["validator"].validate_loop_evidence(current["run"], current["codex_home"])

    def test_tracked_deletion_is_reviewable_as_missing_record(self, current: dict[str, object]) -> None:
        """Keep valid source deletions reviewable even though the worktree path is absent.

        A tracked file removed from the worktree is captured as a `missing` record, so reviewing its deletion is valid.
        """
        (_path_of(current, "repository") / "widget.py").unlink()
        source = _bind_scope(current, "widget.py", ["widget.py"])
        assert [(record["path"], record["kind"]) for record in source["files"]] == [("widget.py", "missing")]

        current["validator"].validate_loop_evidence(current["run"], current["codex_home"])

    def test_binary_source_requires_review_limitation(self, current: dict[str, object]) -> None:
        """Encoded binary data must not be reported as source reviewed with no limitation.

        A binary `widget.py` is captured as base64; the reviewer cannot have read it, so the result must record that
        limitation.
        """
        (_path_of(current, "repository") / "widget.py").write_bytes(b"\xff\x00binary")
        source = _bind_scope(current, "widget.py", ["widget.py"])
        assert source["files"][0]["encoding"] == "base64"

        with pytest.raises(ValueError, match="loop-evidence-binary-source-limit-missing"):
            current["validator"].validate_loop_evidence(current["run"], current["codex_home"])


def test_preflight_rejects_unrepresented_primary_scope_before_dispatch(tmp_path: Path) -> None:
    """An empty literal source capture must fail before sending a reviewer any context.

    The plan scopes `never-existed.py`, which captures no source; preflight must refuse it instead of dispatching a
    reviewer with an empty frozen source.
    """
    validator = _validator()
    repository = _repository(tmp_path)
    scope = ["never-existed.py"]
    snapshot = validator.capture_source_snapshot(repository, scope)
    assert snapshot["files"] == []
    source = validator._canonical_source_bytes(snapshot)
    diff = b""
    request_text = json.dumps(REQUEST, sort_keys=True, ensure_ascii=True).encode()
    context = b"Review request:\n" + request_text + b"\nFrozen source:\n" + source + b"\nFrozen diff:\n\n"
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "source.json").write_bytes(source)
    (plan_dir / "diff.patch").write_bytes(diff)
    (plan_dir / "context.txt").write_bytes(context)
    write_json(
        plan_dir / "plan.json",
        {
            "source_path": "source.json",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "diff_path": "diff.patch",
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "nodes": [
                {
                    "role_id": "challenger",
                    "context_path": "context.txt",
                    "context_sha256": hashlib.sha256(context).hexdigest(),
                }
            ],
        },
    )
    write_json(
        plan_dir / "request.json",
        {
            "schema_version": 2,
            "origin": {"kind": "first-run", "caller_run": None, "prior_ledger_sha256": None},
            "repository": repository.resolve().as_posix(),
            "scope_paths": scope,
            "current_source_path": "source.json",
            "request": REQUEST,
            "supporting_paths": [],
            "current_supporting_source_path": None,
        },
    )

    with pytest.raises(ValueError, match="loop-evidence-primary-source-incomplete"):
        validator.preflight_review_plan(plan_dir / "plan.json", plan_dir / "request.json", None)
