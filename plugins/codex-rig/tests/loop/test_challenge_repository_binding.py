"""Bind coordinator artifact checks to a caller-selected repository outside the evidence files."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from _final_handoff_support import load_shared_validator
from test_adversarial_loop_artifacts import (
    _bind_handoff,
    _build_real_chunked_review,
    _chunked_result,
    _load_handoff,
    _set_chunk_only,
    _set_chunk_review_outcome,
    _write_loop,
)
from test_scoped_coordinator_validation import _scoped_fixture


@pytest.mark.parametrize("stopped", [False, True])
def test_chunk_check_forwards_caller_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: bool) -> None:
    """Clean and stopped checker commands receive the trusted input rather than a manifest-selected path."""
    validator = load_shared_validator()
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    (chunks / "chunks.json").write_text('{"schema_version":1,"repository":"untrusted"}', encoding="utf-8")
    (chunks / "results.json").write_text("{}", encoding="utf-8")
    repository = tmp_path / "trusted"
    repository.mkdir()
    checker = Mock(return_value=subprocess.CompletedProcess([], 0, '{"runs":[]}', ""))
    monkeypatch.setattr(validator.subprocess, "run", checker)

    assert validator._validate_challenge_scope(
        {
            "challenge_scope": {
                "mode": "chunked",
                "manifest_path": "chunks/chunks.json",
                "results_path": "chunks/results.json",
            }
        },
        tmp_path,
        repository=repository,
        summary=True,
        stopped=stopped,
    ) == {"runs": []}

    argv = checker.call_args.args[0]
    assert argv[2] == ("check-stopped" if stopped else "check")
    assert argv[argv.index("--repository") + 1] == str(repository)


@pytest.mark.integration
def test_scoped_repository_is_independent_of_invoking_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real checker accepts a registered mirror from outside Git and rejects an unrelated trusted root."""
    validator, manifest, context = _scoped_fixture(tmp_path, monkeypatch, pinned=True)
    origin = Path(manifest["origin_repository"])
    monkeypatch.chdir(tmp_path)

    summary = validator._validate_challenge_scope(
        {"challenge_scope": context["scope"]}, tmp_path, repository=origin, summary=True
    )

    assert len(summary["runs"]) == 2
    with pytest.raises(SystemExit, match="chunk-source-repository-mismatch"):
        validator._validate_challenge_scope(
            {"challenge_scope": context["scope"]}, tmp_path, repository=tmp_path, summary=True
        )


@pytest.mark.integration
def test_stopped_repository_is_independent_of_invoking_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopped legacy coverage accepts a caller-selected checkout and rejects a different trusted root."""
    review = _build_real_chunked_review(tmp_path, monkeypatch)
    validator = load_shared_validator()
    (review.chunks / "results.json").write_text(
        json.dumps(review.result_map | {"chunks": [review.result_map["chunks"][0]], "interactions": [], "groups": []}),
        encoding="utf-8",
    )
    metadata = {
        "challenge_scope": {
            "mode": "chunked",
            "manifest_path": "chunks/chunks.json",
            "results_path": "chunks/results.json",
        }
    }
    monkeypatch.chdir(tmp_path)

    summary = validator._validate_challenge_scope(
        metadata, tmp_path, repository=review.repository, summary=True, stopped=True
    )

    assert len(summary["runs"]) == 1
    assert summary["pending_chunks"] == ["chunk-002", "chunk-003"]
    with pytest.raises(SystemExit, match="chunk-source-repository-mismatch"):
        validator._validate_challenge_scope(metadata, tmp_path, repository=tmp_path, summary=True, stopped=True)


@pytest.mark.integration
@pytest.mark.parametrize("all_errors", [False, True])
def test_public_validation_uses_trusted_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, all_errors: bool
) -> None:
    """Both public validation modes check real coordinator evidence from a directory outside its repository."""
    review = _build_real_chunked_review(tmp_path, monkeypatch)
    candidate = _write_loop(tmp_path, monkeypatch)
    handoff = _load_handoff(tmp_path)
    _set_chunk_only(handoff, review.summary["runs"])
    _set_chunk_review_outcome(handoff, "All declared child and interaction reviews are clean.")
    result = _chunked_result(candidate, review.summary, review.manifest["origin"])
    _bind_handoff(tmp_path, candidate, result, handoff)
    monkeypatch.setenv("CODEX_HOME", str(review.session_home))
    monkeypatch.chdir(tmp_path)
    validator = load_shared_validator()
    validate = validator.collect_errors if all_errors else validator.validate

    report = validate("challenge-resolve", tmp_path, candidate, repository=review.repository)

    assert report == ({"status": "pass", "errors": [], "not_run": []} if all_errors else None)


@pytest.mark.parametrize("all_errors", [False, True])
def test_cli_forwards_repository_to_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], all_errors: bool
) -> None:
    """Both CLI modes preserve the independently supplied repository through the validation-step pipeline."""
    validator = load_shared_validator()
    result = tmp_path / "result.json"
    check = Mock()
    steps = Mock(return_value=[validator._ValidationStep("adversarial-loop", check)])
    monkeypatch.setattr(validator, "_validation_steps", steps)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate-artifacts.py",
            "--skill",
            "challenge-resolve",
            "--out",
            str(tmp_path),
            "--result",
            str(result),
            "--repository",
            str(tmp_path),
        ]
        + (["--all-errors"] if all_errors else []),
    )

    assert validator.main() == 0
    steps.assert_called_once_with("challenge-resolve", tmp_path, result, repository=tmp_path)
    check.assert_called_once_with()
    assert not all_errors or json.loads(capsys.readouterr().out)["status"] == "pass"
