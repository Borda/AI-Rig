"""Verify bounded challenge chunks cover changed source and reject stale evidence."""

from __future__ import annotations

import json
import hashlib
import importlib.util
import itertools
import subprocess
import sys
from pathlib import Path

import pytest


CHUNKER = Path(__file__).resolve().parents[1] / "skills" / "challenge-resolve" / "chunk_diff.py"
REQUEST_ARGS = ("--goal", "Review changed files", "--specification", "Fixture contract", "--done-when", "Clean review")


@pytest.fixture
def changed_repository(tmp_path: Path) -> Path:
    """Return a repository with tracked edits, a deletion, and untracked source."""
    repository = tmp_path / "repository"
    repository.mkdir()
    for name in ("alpha.py", "beta.py", "removed.py"):
        (repository / name).write_text(f"VALUE = '{name}'\n", encoding="utf-8", newline="\n")
    for args in (
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"),
    ):
        subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)
    (repository / "alpha.py").write_text("VALUE = 'changed alpha'\n", encoding="utf-8", newline="\n")
    (repository / "beta.py").write_text("VALUE = 'changed beta'\n", encoding="utf-8", newline="\n")
    (repository / "removed.py").unlink()
    (repository / "new.py").write_text("VALUE = 'new'\n", encoding="utf-8", newline="\n")
    return repository


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the packaged chunker through its public CLI."""
    return subprocess.run([sys.executable, str(CHUNKER), *args], text=True, capture_output=True, check=False)


def _prior_ledger() -> dict[str, object]:
    """Return a structurally valid earlier review with one open signature."""
    snapshot = {"revision": "test-revision", "diff_digest": "a" * 64}
    return {
        "schema_version": 1,
        "implementation_author": "solver",
        "current_snapshot": snapshot,
        "rounds": [
            {
                "index": 1,
                "reviewer": {"identity": "challenger", "independent": True},
                "snapshot": snapshot,
                "report_path": "review.json",
                "findings": [
                    {
                        "signature": "caller-origin",
                        "tier": "high",
                        "structural": False,
                        "disposition": "open",
                        "evidence": ["Caller record absent"],
                    }
                ],
            }
        ],
    }


@pytest.mark.packaging
def test_declared_continuation_requires_prior_ledger(changed_repository: Path, tmp_path: Path) -> None:
    """A declared resumed review cannot silently discard its caller's findings."""
    caller_run = tmp_path / "caller-run"
    caller_run.mkdir()
    (caller_run / "loop-ledger.json").write_text(json.dumps(_prior_ledger()), encoding="utf-8", newline="\n")
    out = tmp_path / "continued-review"
    planned = _run(
        "plan",
        "--repository",
        str(changed_repository),
        "--out",
        str(out),
        "--budget-bytes",
        "900",
        "--caller-run",
        str(caller_run),
        *REQUEST_ARGS,
    )
    assert planned.returncode != 0
    assert "prior-ledger-required-for-continuation" in planned.stderr


@pytest.mark.packaging
def test_fresh_chunk_plan_without_prior_ledger(changed_repository: Path, tmp_path: Path) -> None:
    """A first review can plan and validate without an earlier ledger."""
    fresh_out = tmp_path / "fresh-review"
    fresh = _run(
        "plan",
        "--repository",
        str(changed_repository),
        "--out",
        str(fresh_out),
        "--budget-bytes",
        "900",
        *REQUEST_ARGS,
    )
    assert fresh.returncode == 0, fresh.stderr
    manifest_path = fresh_out / "chunks.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["prior_findings"] == {"ledger_path": None, "ledger_sha256": None, "coverage": []}
    assert _run("check", "--manifest", str(manifest_path)).returncode == 0


@pytest.mark.packaging
@pytest.mark.parametrize("scope_mode", ["all", "changed"])
def test_chunk_plan_retains_staged_deletion(changed_repository: Path, tmp_path: Path, scope_mode: str) -> None:
    """Include a HEAD file removed from the index in each requested scope mode."""
    subprocess.run(["git", "rm", "removed.py"], cwd=changed_repository, check=True, capture_output=True)
    out = tmp_path / f"review-{scope_mode}"

    planned = _run(
        "plan",
        "--repository",
        str(changed_repository),
        "--out",
        str(out),
        "--scope-path",
        "removed.py",
        "--scope-mode",
        scope_mode,
        "--budget-bytes",
        "900",
        *REQUEST_ARGS,
    )

    assert planned.returncode == 0, planned.stderr
    manifest_path = out / "chunks.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["inventory"] == ["removed.py"]
    chunk = manifest["chunks"][0]
    source = json.loads((out / chunk["source_path"]).read_text(encoding="utf-8"))
    assert source["files"] == [
        {
            "path": "removed.py",
            "kind": "missing",
            "sha256": None,
            "executable": False,
            "encoding": "utf-8",
            "content": "",
        }
    ]
    assert b"deleted file mode" in (out / chunk["diff_path"]).read_bytes()
    assert _run("check", "--manifest", str(manifest_path)).returncode == 0


@pytest.mark.packaging
def test_checker_rejects_declared_continuation_without_prior_ledger(changed_repository: Path, tmp_path: Path) -> None:
    """A continuation declaration cannot pass source checking with null prior evidence."""
    caller_run = tmp_path / "caller-run"
    caller_run.mkdir()
    (caller_run / "loop-ledger.json").write_text(json.dumps(_prior_ledger()), encoding="utf-8", newline="\n")
    fresh_out = tmp_path / "fresh-review"
    fresh = _run(
        "plan",
        "--repository",
        str(changed_repository),
        "--out",
        str(fresh_out),
        "--budget-bytes",
        "900",
        *REQUEST_ARGS,
    )
    assert fresh.returncode == 0, fresh.stderr
    manifest_path = fresh_out / "chunks.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest["origin"] = {"kind": "continuation", "caller_run": str(caller_run)}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    checked = _run("check", "--manifest", str(manifest_path))
    assert checked.returncode != 0
    assert "prior-ledger-required-for-continuation" in checked.stderr


@pytest.mark.packaging
def test_prior_findings_require_complete_source_and_review_assignment(changed_repository: Path, tmp_path: Path) -> None:
    """Continuation planning cannot omit a prior signature or its needed source."""
    prior = tmp_path / "prior" / "loop-ledger.json"
    prior.parent.mkdir()
    prior.write_text(json.dumps(_prior_ledger()))
    coverage = tmp_path / "coverage.json"
    coverage.write_text("[]")
    planned = _run(
        "plan",
        "--repository",
        str(changed_repository),
        "--out",
        str(tmp_path / "review"),
        "--budget-bytes",
        "900",
        "--prior-ledger",
        str(prior),
        "--prior-coverage",
        str(coverage),
        "--caller-run",
        str(prior.parent),
        *REQUEST_ARGS,
    )
    assert planned.returncode != 0
    assert "prior-coverage-invalid" in planned.stderr


@pytest.mark.packaging
def test_unsorted_prior_paths_rejected_before_chunk_output(changed_repository: Path, tmp_path: Path) -> None:
    """Planning must reject noncanonical prior paths before writing any review artifact."""
    prior = tmp_path / "prior" / "loop-ledger.json"
    prior.parent.mkdir()
    prior.write_text(json.dumps(_prior_ledger()), encoding="utf-8", newline="\n")
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            [
                {
                    "signature": "caller-origin",
                    "source_paths": ["beta.py", "alpha.py"],
                    "review_kind": "chunk",
                    "chunk_ids": ["chunk-001"],
                }
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    out = tmp_path / "review"
    planned = _run(
        "plan",
        "--repository",
        str(changed_repository),
        "--out",
        str(out),
        "--budget-bytes",
        "900",
        "--prior-ledger",
        str(prior),
        "--prior-coverage",
        str(coverage),
        "--caller-run",
        str(prior.parent),
        *REQUEST_ARGS,
    )
    assert planned.returncode != 0
    assert "prior-coverage-invalid" in planned.stderr
    assert not out.exists()


@pytest.mark.parametrize(
    ("path", "expected_issue"),
    [
        pytest.param("missing.py", "prior-source-coverage-incomplete", id="absent-path"),
        pytest.param("removed.py", None, id="tracked-deletion"),
    ],
)
def test_prior_signature_visibility_requires_source_record(
    changed_repository: Path, tmp_path: Path, path: str, expected_issue: str | None
) -> None:
    """Only a captured file or tracked-deletion record satisfies prior source coverage."""
    spec = importlib.util.spec_from_file_location("challenge_chunk_diff", CHUNKER)
    assert spec is not None and spec.loader is not None
    chunker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chunker)
    run = tmp_path / "runs" / "chunk-001"
    run.mkdir(parents=True)
    snapshot = chunker.capture_source_snapshot(changed_repository, ["alpha.py", path])
    expected_paths = {"alpha.py"} if path == "missing.py" else {"alpha.py", "removed.py"}
    assert {record["path"] for record in snapshot["files"]} == expected_paths
    if path == "removed.py":
        assert next(record for record in snapshot["files"] if record["path"] == path)["kind"] == "missing"
    (run / "current-source.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (run / "result.json").write_text('{"status":"pass"}', encoding="utf-8", newline="\n")
    (run / "loop-evidence.json").write_text(
        json.dumps(
            {
                "scope_paths": ["alpha.py", path],
                "current_source_path": "current-source.json",
                "supporting_paths": [],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    ledger = _prior_ledger()
    ledger["rounds"][0]["findings"][0]["disposition"] = "verified-fixed"
    (run / "loop-ledger.json").write_text(json.dumps(ledger), encoding="utf-8", newline="\n")
    item = {
        "signature": "caller-origin",
        "source_paths": [path],
        "review_kind": "chunk",
        "chunk_ids": ["chunk-001"],
    }
    results = {
        "chunks": [{"id": "chunk-001", "result_path": "runs/chunk-001/result.json"}],
        "interactions": [],
        "groups": [],
    }

    assert chunker._prior_result_issue(tmp_path, results, item, _prior_ledger()["rounds"][0]["findings"][0]) == (
        expected_issue
    )


@pytest.mark.packaging
def test_prior_finding_missing_in_assigned_review_blocks_clean(changed_repository: Path, tmp_path: Path) -> None:
    """A child clean verdict cannot silently close an earlier independent finding."""
    prior = tmp_path / "prior" / "loop-ledger.json"
    prior.parent.mkdir()
    prior.write_text(json.dumps(_prior_ledger()))
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            [
                {
                    "signature": "caller-origin",
                    "source_paths": ["alpha.py"],
                    "review_kind": "chunk",
                    "chunk_ids": ["chunk-001"],
                }
            ]
        )
    )
    out = tmp_path / "review"
    counterexample = {
        "signature": "caller-origin",
        "tier": "high",
        "structural": False,
        "evidence": ["Caller record absent"],
        "source_paths": ["alpha.py"],
    }
    expected_specification = "Fixture contract\nPrior findings to reassess:\n" + json.dumps(
        [counterexample], sort_keys=True, ensure_ascii=True
    )
    planned = _run(
        "plan",
        "--repository",
        str(changed_repository),
        "--out",
        str(out),
        "--budget-bytes",
        "900",
        "--prior-ledger",
        str(prior),
        "--prior-coverage",
        str(coverage),
        "--caller-run",
        str(prior.parent),
        *REQUEST_ARGS,
    )
    assert planned.returncode == 0, planned.stderr
    manifest = json.loads((out / "chunks.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["origin"] == {"kind": "continuation", "caller_run": str(prior.parent)}
    assert manifest["prior_findings"]["coverage"][0]["signature"] == "caller-origin"
    assert manifest["request"]["specification"] == expected_specification
    repeated = tmp_path / "repeated-review"
    replanned = _run(
        "plan",
        "--repository",
        str(changed_repository),
        "--out",
        str(repeated),
        "--budget-bytes",
        "900",
        "--prior-ledger",
        str(prior),
        "--prior-coverage",
        str(coverage),
        "--caller-run",
        str(prior.parent),
        "--goal",
        "Review changed files",
        "--specification",
        expected_specification,
        "--done-when",
        "Clean review",
    )
    assert replanned.returncode == 0, replanned.stderr
    assert json.loads((repeated / "chunks.json").read_text())["request"]["specification"] == expected_specification
    invalid = json.loads(json.dumps(manifest))
    invalid["schema_version"] = 5
    (out / "chunks.json").write_text(json.dumps(invalid), encoding="utf-8", newline="\n")
    rejected = _run("check", "--manifest", str(out / "chunks.json"))
    assert rejected.returncode != 0
    assert "chunk-manifest-invalid" in rejected.stderr
    manifest["request"]["specification"] = "Fixture contract"
    (out / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    tampered = _run("check", "--manifest", str(out / "chunks.json"))
    assert tampered.returncode != 0
    assert "prior-request-block-missing" in tampered.stderr
    for index, invalid_path in enumerate(("C:caller.py", "caller\x00.py")):
        coverage.write_text(
            json.dumps(
                [
                    {
                        "signature": "caller-origin",
                        "source_paths": [invalid_path],
                        "review_kind": "chunk",
                        "chunk_ids": ["chunk-001"],
                    }
                ]
            )
        )
        invalid = _run(
            "plan",
            "--repository",
            str(changed_repository),
            "--out",
            str(tmp_path / f"invalid-{index}"),
            "--budget-bytes",
            "900",
            "--prior-ledger",
            str(prior),
            "--prior-coverage",
            str(coverage),
            "--caller-run",
            str(prior.parent),
            *REQUEST_ARGS,
        )
        assert invalid.returncode != 0
        assert "prior-coverage-invalid" in invalid.stderr


@pytest.mark.packaging
def test_continuation_check_rechecks_caller_ledger_bytes(changed_repository: Path, tmp_path: Path) -> None:
    """A copied prior ledger cannot certify a caller ledger changed after planning."""
    caller_run = tmp_path / "caller-run"
    caller_run.mkdir()
    prior = caller_run / "loop-ledger.json"
    prior.write_text(json.dumps(_prior_ledger()), encoding="utf-8", newline="\n")
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            [
                {
                    "signature": "caller-origin",
                    "source_paths": ["alpha.py"],
                    "review_kind": "chunk",
                    "chunk_ids": ["chunk-001"],
                }
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    out = tmp_path / "review"
    planned = _run(
        "plan",
        "--repository",
        str(changed_repository),
        "--out",
        str(out),
        "--budget-bytes",
        "900",
        "--caller-run",
        str(caller_run),
        "--prior-ledger",
        str(prior),
        "--prior-coverage",
        str(coverage),
        *REQUEST_ARGS,
    )
    assert planned.returncode == 0, planned.stderr
    manifest_path = out / "chunks.json"
    assert _run("check", "--manifest", str(manifest_path)).returncode == 0
    changed = _prior_ledger()
    changed["rounds"][0]["findings"][0]["evidence"] = ["Caller record still absent"]
    prior.write_text(json.dumps(changed), encoding="utf-8", newline="\n")
    checked = _run("check", "--manifest", str(manifest_path))
    assert checked.returncode != 0
    assert "caller-ledger-changed" in checked.stderr
    prior.unlink()
    missing = _run("check", "--manifest", str(manifest_path))
    assert missing.returncode != 0
    assert "caller-ledger-unavailable" in missing.stderr


@pytest.mark.packaging
def test_prior_result_cannot_reclassify_verified_signature(changed_repository: Path, tmp_path: Path) -> None:
    """A child disposition cannot silently lower a caller finding's severity or structural status."""
    spec = importlib.util.spec_from_file_location("challenge_chunk_diff", CHUNKER)
    assert spec is not None and spec.loader is not None
    chunker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chunker)
    prior = _prior_ledger()
    prior["rounds"][0]["findings"][0]["structural"] = True
    (tmp_path / "prior-loop-ledger.json").write_text(json.dumps(prior), encoding="utf-8", newline="\n")
    child = json.loads(json.dumps(prior))
    child_finding = child["rounds"][0]["findings"][0]
    child_finding.update(tier="low", structural=False, disposition="verified-fixed")
    assert chunker.validate_ledger(child) == []
    run = tmp_path / "runs" / "child"
    run.mkdir(parents=True)
    (run / "loop-ledger.json").write_text(json.dumps(child), encoding="utf-8", newline="\n")
    source = chunker.capture_source_snapshot(changed_repository, ["alpha.py"])
    (run / "current-source.json").write_text(
        json.dumps(source, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (run / "loop-evidence.json").write_text(
        json.dumps({"scope_paths": ["alpha.py"], "current_source_path": "current-source.json"}),
        encoding="utf-8",
        newline="\n",
    )
    (run / "result.json").write_text("{}", encoding="utf-8", newline="\n")
    coverage = {
        "signature": "caller-origin",
        "source_paths": ["alpha.py"],
        "review_kind": "chunk",
        "chunk_ids": ["chunk-001"],
    }
    manifest = {"prior_findings": {"ledger_path": "prior-loop-ledger.json", "coverage": [coverage]}}
    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps(
            {"chunks": [{"id": "chunk-001", "result_path": "runs/child/result.json"}], "interactions": [], "groups": []}
        ),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ValueError, match="prior-finding-attribution-mismatch:caller-origin"):
        chunker._check_prior_results(tmp_path, manifest, results_path)


@pytest.mark.packaging
def test_chunk_plan_covers_changed_files_and_rechecks_current_source(changed_repository: Path, tmp_path: Path) -> None:
    """A size split must retain deletion and untracked bytes without overlap."""
    out = tmp_path / "review"
    planned = _run(
        "plan", "--repository", str(changed_repository), "--out", str(out), "--budget-bytes", "900", *REQUEST_ARGS
    )
    assert planned.returncode == 0, planned.stderr
    manifest = json.loads((out / "chunks.json").read_text(encoding="utf-8"))
    assert manifest["inventory"] == ["alpha.py", "beta.py", "new.py", "removed.py"]
    assert len(manifest["chunks"]) >= 2
    assert sorted(path for chunk in manifest["chunks"] for path in chunk["scope_paths"]) == manifest["inventory"]
    assert any(
        record["path"] == "new.py"
        for chunk in manifest["chunks"]
        for record in json.loads((out / chunk["source_path"]).read_text(encoding="utf-8"))["files"]
    )
    assert _run("check", "--manifest", str(out / "chunks.json")).returncode == 0
    for invalid_version in (True, 2, 3, 4, 5):
        invalid = manifest | {"schema_version": invalid_version}
        (out / "chunks.json").write_text(json.dumps(invalid), encoding="utf-8", newline="\n")
        rejected = _run("check", "--manifest", str(out / "chunks.json"))
        assert rejected.returncode != 0
        assert "chunk-manifest-invalid" in rejected.stderr
    (out / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

    (changed_repository / "alpha.py").write_text("VALUE = 'later change'\n", encoding="utf-8", newline="\n")
    stale = _run("check", "--manifest", str(out / "chunks.json"))
    assert stale.returncode != 0
    assert "chunk-source-changed" in stale.stderr


@pytest.mark.packaging
def test_oversized_file_does_not_hide_other_changed_files(changed_repository: Path, tmp_path: Path) -> None:
    """An oversized single file remains visible while smaller files get chunks."""
    (changed_repository / "alpha.py").write_text("X = '" + "a" * 4000 + "'\n", encoding="utf-8", newline="\n")
    out = tmp_path / "review"
    planned = _run(
        "plan", "--repository", str(changed_repository), "--out", str(out), "--budget-bytes", "900", *REQUEST_ARGS
    )
    assert planned.returncode == 0, planned.stderr
    manifest = json.loads((out / "chunks.json").read_text(encoding="utf-8"))
    assert manifest["inventory"] == ["alpha.py", "beta.py", "new.py", "removed.py"]
    assert [chunk["scope_paths"] for chunk in manifest["chunks"] if chunk["oversize"]] == [["alpha.py"]]
    assert _run("check", "--manifest", str(out / "chunks.json")).returncode == 0


@pytest.mark.packaging
def test_result_map_cannot_claim_clean_without_each_chunk(changed_repository: Path, tmp_path: Path) -> None:
    """Reject a final coverage claim that omits a reviewed chunk."""
    out = tmp_path / "review"
    assert (
        _run(
            "plan", "--repository", str(changed_repository), "--out", str(out), "--budget-bytes", "900", *REQUEST_ARGS
        ).returncode
        == 0
    )
    results = out / "results.json"
    results.write_text('{"schema_version":1,"chunks":[]}\n', encoding="utf-8", newline="\n")

    checked = _run("check", "--manifest", str(out / "chunks.json"), "--results", str(results))

    assert checked.returncode != 0
    assert "chunk-results-invalid" in checked.stderr


@pytest.mark.packaging
def test_stopped_summary_binds_failed_child_and_reports_pending_slots(
    changed_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed child remains visible while unreviewed chunks and interactions stay pending."""
    spec = importlib.util.spec_from_file_location("challenge_chunk_diff", CHUNKER)
    assert spec is not None and spec.loader is not None
    chunker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chunker)
    out = tmp_path / "review"
    assert (
        _run(
            "plan", "--repository", str(changed_repository), "--out", str(out), "--budget-bytes", "900", *REQUEST_ARGS
        ).returncode
        == 0
    )
    manifest_path = out / "chunks.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chunks = manifest["chunks"]
    assert len(chunks) >= 2
    first = chunks[0]
    run = out / "runs" / first["id"]
    run.mkdir(parents=True)
    (run / "current-source.json").write_bytes((out / first["source_path"]).read_bytes())
    (run / "current.diff").write_bytes((out / first["diff_path"]).read_bytes())
    (run / "loop-evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "repository": changed_repository.resolve().as_posix(),
                "scope_paths": first["scope_paths"],
                "current_source_path": "current-source.json",
                "request": manifest["request"],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    result_path = run / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "metadata": {
                    "adversarial_loop": {
                        "status": "stopped",
                        "reason": "plateau",
                        "scores": [6],
                        "rounds": [{"decision": "plateau"}],
                    }
                },
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    results_path = out / "results.json"
    results_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "chunks": [{"id": first["id"], "result_path": f"runs/{first['id']}/result.json"}],
                "interactions": [],
                "groups": [],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    validator_calls = []
    real_run = chunker.subprocess.run

    def validate_or_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Record the public artifact validator while preserving local Git inspection."""
        if len(command) > 1 and Path(command[1]).name == "validate-artifacts.py":
            validator_calls.append(command)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return real_run(command, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(chunker.subprocess, "run", validate_or_run)
        summary = chunker.check_stopped(manifest_path, results_path)
        assert summary == {
            "schema_version": 1,
            "status": "stopped",
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "results_sha256": hashlib.sha256(results_path.read_bytes()).hexdigest(),
            "runs": [
                {
                    "result_path": f"runs/{first['id']}/result.json",
                    "scores": [6],
                    "decision": "plateau",
                    "reason": "plateau",
                }
            ],
            "pending_chunks": [chunk["id"] for chunk in chunks[1:]],
            "pending_interactions": [
                list(pair) for pair in itertools.combinations((chunk["id"] for chunk in chunks), 2)
            ],
            "pending_groups": [],
            "pending_prior_signatures": [],
        }
        assert len(validator_calls) == 1
        zero_round_result = json.loads(result_path.read_text(encoding="utf-8"))
        zero_round_result["metadata"]["adversarial_loop"].update(
            scores=[], rounds=[], reason="independence-unavailable"
        )
        result_path.write_text(json.dumps(zero_round_result), encoding="utf-8", newline="\n")
        with pytest.raises(ValueError, match="chunk-result-review-missing"):
            chunker.check_stopped(manifest_path, results_path)
        zero_round_result["metadata"]["adversarial_loop"].update(
            scores=[6], rounds=[{"decision": "plateau"}], reason="plateau"
        )
        result_path.write_text(json.dumps(zero_round_result), encoding="utf-8", newline="\n")
        with pytest.raises(ValueError, match="chunk-result-request-mismatch"):
            evidence_path = run / "loop-evidence.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["request"]["goal"] = "unrelated review"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8", newline="\n")
            chunker.check_stopped(manifest_path, results_path)
        evidence["request"] = manifest["request"]
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8", newline="\n")
        patch.setattr(
            sys,
            "argv",
            [
                str(CHUNKER),
                "check-stopped",
                "--manifest",
                str(manifest_path),
                "--results",
                str(results_path),
                "--summary",
            ],
        )
        assert chunker.main() == 0
        assert json.loads(capsys.readouterr().out) == summary
        with pytest.raises(ValueError, match="chunk-results-invalid"):
            chunker.check(manifest_path, results_path)
        first_pair = [chunks[0]["id"], chunks[1]["id"]]
        pending_map = json.loads(results_path.read_text(encoding="utf-8"))
        pending_map["interactions"] = [
            {
                "chunk_ids": first_pair,
                "decision": "reviewed",
                "evidence": "The pair shares a contract, but review has not completed",
                "result_path": None,
            }
        ]
        if len(chunks) >= 3:
            pending_map["groups"] = [
                {
                    "chunk_ids": [chunk["id"] for chunk in chunks[:3]],
                    "evidence": "The group shares a contract, but review has not completed",
                    "result_path": None,
                }
            ]
        results_path.write_text(json.dumps(pending_map), encoding="utf-8", newline="\n")
        pending_summary = chunker.check_stopped(manifest_path, results_path)
        assert first_pair in pending_summary["pending_interactions"]
        assert pending_summary["pending_groups"] == [entry["chunk_ids"] for entry in pending_map["groups"]]
        pending_map["chunks"][0]["result_path"] = "../outside/result.json"
        results_path.write_text(json.dumps(pending_map), encoding="utf-8", newline="\n")
        with pytest.raises(ValueError, match="chunk-artifact-path-invalid"):
            chunker.check_stopped(manifest_path, results_path)


@pytest.mark.packaging
def test_result_map_requires_independent_cross_chunk_reviews(changed_repository: Path, tmp_path: Path) -> None:
    """A clean result per file chunk cannot establish review of their interactions."""
    out = tmp_path / "review"
    assert (
        _run(
            "plan", "--repository", str(changed_repository), "--out", str(out), "--budget-bytes", "900", *REQUEST_ARGS
        ).returncode
        == 0
    )
    manifest = json.loads((out / "chunks.json").read_text(encoding="utf-8"))
    assert len(manifest["chunks"]) >= 2
    results = out / "results.json"
    results.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "chunks": [
                    {"id": chunk["id"], "result_path": f"runs/{chunk['id']}/result.json"}
                    for chunk in manifest["chunks"]
                ],
                "interactions": [],
                "groups": [],
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    checked = _run("check", "--manifest", str(out / "chunks.json"), "--results", str(results))

    assert checked.returncode != 0
    assert "chunk-interaction-coverage-invalid" in checked.stderr


@pytest.mark.packaging
def test_no_interaction_decision_requires_source_rationale(changed_repository: Path, tmp_path: Path) -> None:
    """An empty no-interaction assertion cannot waive independent pair review."""
    out = tmp_path / "review"
    assert (
        _run(
            "plan", "--repository", str(changed_repository), "--out", str(out), "--budget-bytes", "900", *REQUEST_ARGS
        ).returncode
        == 0
    )
    chunks = json.loads((out / "chunks.json").read_text(encoding="utf-8"))["chunks"]
    assert len(chunks) >= 2
    interactions = [
        {
            "chunk_ids": [left["id"], right["id"]],
            "decision": "no-interaction",
            "evidence": "  ",
            "result_path": "runs/interactions/result.json",
        }
        for left, right in itertools.combinations(chunks, 2)
    ]
    results = out / "results.json"
    results.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "chunks": [{"id": chunk["id"], "result_path": f"runs/{chunk['id']}/result.json"} for chunk in chunks],
                "interactions": interactions,
                "groups": [],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )

    checked = _run("check", "--manifest", str(out / "chunks.json"), "--results", str(results))

    assert checked.returncode != 0
    assert "chunk-interaction-decision-invalid" in checked.stderr


@pytest.mark.packaging
def test_parent_interaction_claims_do_not_establish_independent_assessment(
    changed_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject plausible parent decisions without an authenticated interaction assessment."""
    spec = importlib.util.spec_from_file_location("challenge_chunk_diff", CHUNKER)
    assert spec is not None and spec.loader is not None
    chunker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chunker)
    out = tmp_path / "review"
    assert (
        _run(
            "plan", "--repository", str(changed_repository), "--out", str(out), "--budget-bytes", "900", *REQUEST_ARGS
        ).returncode
        == 0
    )
    manifest = json.loads((out / "chunks.json").read_text(encoding="utf-8"))
    chunks = manifest["chunks"]
    assert len(chunks) >= 3
    results = out / "results.json"
    results.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "chunks": [{"id": chunk["id"], "result_path": f"runs/{chunk['id']}/result.json"} for chunk in chunks],
                "interactions": [
                    {
                        "chunk_ids": [left["id"], right["id"]],
                        "decision": "no-interaction",
                        "evidence": "Parent claims no shared behavior",
                    }
                    for left, right in itertools.combinations(chunks, 2)
                ],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(chunker, "_check_clean_run", lambda *args: None)

    with pytest.raises(ValueError, match="chunk-results-invalid"):
        chunker.check(out / "chunks.json", results)


@pytest.mark.packaging
def test_cross_chunk_review_binds_both_current_source_files(
    changed_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Child and interaction reviews must retain the coordinator request and current source."""
    spec = importlib.util.spec_from_file_location("challenge_chunk_diff", CHUNKER)
    assert spec is not None and spec.loader is not None
    chunker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chunker)
    out = tmp_path / "review"
    coordinator_request = {
        "goal": "Review the complete changed-file contract",
        "specification": "Every changed file must preserve the fixture contract",
        "done_when": "All chunks and their interactions are independently clean",
    }
    assert (
        _run(
            "plan",
            "--repository",
            str(changed_repository),
            "--out",
            str(out),
            "--budget-bytes",
            "900",
            "--goal",
            coordinator_request["goal"],
            "--specification",
            coordinator_request["specification"],
            "--done-when",
            coordinator_request["done_when"],
        ).returncode
        == 0
    )
    manifest = json.loads((out / "chunks.json").read_text(encoding="utf-8"))
    assert manifest["request"] == coordinator_request
    chunks = manifest["chunks"]
    assert len(chunks) >= 3
    result_map = {"schema_version": 1, "chunks": [], "interactions": [], "groups": []}
    for chunk in chunks:
        result_map["chunks"].append({"id": chunk["id"], "result_path": f"runs/{chunk['id']}/result.json"})
        run = out / "runs" / chunk["id"]
        run.mkdir(parents=True)
        (run / "current-source.json").write_bytes((out / chunk["source_path"]).read_bytes())
        (run / "current.diff").write_bytes((out / chunk["diff_path"]).read_bytes())
        (run / "loop-evidence.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "repository": changed_repository.resolve().as_posix(),
                    "scope_paths": chunk["scope_paths"],
                    "current_source_path": "current-source.json",
                    "request": coordinator_request,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        (run / "result.json").write_text(
            '{"status":"pass","metadata":{"adversarial_loop":{"reason":"clean","scores":[0],"rounds":[{"decision":"clean"}]}}}',
            encoding="utf-8",
            newline="\n",
        )
    reviewed_pairs = list(itertools.combinations(chunks[:3], 2))[:2]
    shared_paths: set[str] = set()
    for left, right in itertools.combinations(chunks, 2):
        pair = [left["id"], right["id"]]
        if (left, right) not in reviewed_pairs:
            result_map["interactions"].append(
                {
                    "chunk_ids": pair,
                    "decision": "no-interaction",
                    "evidence": f"{left['scope_paths']} and {right['scope_paths']} have no shared interface in this fixture",
                    "result_path": "runs/interactions/result.json",
                }
            )
        else:
            result_map["interactions"].append(
                {
                    "chunk_ids": pair,
                    "decision": "reviewed",
                    "evidence": "The declared changed files share an interface in this fixture",
                    "result_path": "runs/interactions/result.json",
                }
            )
        shared_paths.update([*left["scope_paths"], *right["scope_paths"]])
    result_map["groups"].append(
        {
            "chunk_ids": [chunk["id"] for chunk in chunks[:3]],
            "evidence": "Three chunks jointly define the fixture contract",
            "result_path": "runs/interactions/result.json",
        }
    )
    assert len(shared_paths) == len(manifest["inventory"])
    run = out / "runs" / "interactions"
    run.mkdir(parents=True)
    paths = sorted(shared_paths)
    source, diff = chunker._material(changed_repository, paths)
    (run / "current-source.json").write_bytes(source)
    (run / "current.diff").write_bytes(diff)
    decisions = [
        {key: entry[key] for key in ("chunk_ids", "decision", "evidence")} for entry in result_map["interactions"]
    ] + [
        {"chunk_ids": entry["chunk_ids"], "decision": "reviewed", "evidence": entry["evidence"]}
        for entry in result_map["groups"]
    ]
    request = {
        "goal": coordinator_request["goal"],
        "specification": coordinator_request["specification"]
        + "\nInteraction assessment:\n"
        + json.dumps(decisions, sort_keys=True, ensure_ascii=True),
        "done_when": coordinator_request["done_when"],
    }
    (run / "loop-evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "repository": changed_repository.resolve().as_posix(),
                "scope_paths": paths,
                "current_source_path": "current-source.json",
                "request": request,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    (run / "result.json").write_text(
        '{"status":"pass","metadata":{"adversarial_loop":{"reason":"clean","scores":[0],"rounds":[{"decision":"clean"}]}}}',
        encoding="utf-8",
        newline="\n",
    )
    results = out / "results.json"
    results.write_text(json.dumps(result_map), encoding="utf-8", newline="\n")
    unsupported_results = out / "unsupported-results.json"
    for invalid_version in (True, 3):
        unsupported_results.write_text(
            json.dumps(result_map | {"schema_version": invalid_version}), encoding="utf-8", newline="\n"
        )
        with pytest.raises(ValueError, match="chunk-results-invalid"):
            chunker.check(out / "chunks.json", unsupported_results)

    real_run = subprocess.run

    def validate_or_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Stand in only for the separate challenge artifact validator."""
        if len(command) > 1 and Path(command[1]).name == "validate-artifacts.py":
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return real_run(command, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(chunker.subprocess, "run", validate_or_run)
        summary = chunker.check(out / "chunks.json", results)
        assert summary == {
            "schema_version": 1,
            "manifest_sha256": hashlib.sha256((out / "chunks.json").read_bytes()).hexdigest(),
            "results_sha256": hashlib.sha256(results.read_bytes()).hexdigest(),
            "runs": [
                {"result_path": entry["result_path"], "scores": [0], "decision": "clean"}
                for entry in result_map["chunks"]
            ]
            + [{"result_path": "runs/interactions/result.json", "scores": [0], "decision": "clean"}],
        }
        patch.setattr(
            sys,
            "argv",
            [str(CHUNKER), "check", "--manifest", str(out / "chunks.json"), "--results", str(results), "--summary"],
        )
        assert chunker.main() == 0
        assert json.loads(capsys.readouterr().out) == summary
        original_manifest = (out / "chunks.json").read_bytes()
        original_check_results = chunker._check_results
        patch.setattr(chunker, "_check_results", lambda *args: ([], "validated-in-other-test"))
        prior_bytes = (json.dumps(_prior_ledger()) + "\n").encode()
        (out / "prior-loop-ledger.json").write_bytes(prior_bytes)
        caller_run = tmp_path / "prior"
        caller_run.mkdir()
        (caller_run / "loop-ledger.json").write_bytes(prior_bytes)
        manifest["prior_findings"] = {
            "ledger_path": "prior-loop-ledger.json",
            "ledger_sha256": hashlib.sha256(prior_bytes).hexdigest(),
            "coverage": [
                {
                    "signature": "caller-origin",
                    "source_paths": ["alpha.py"],
                    "review_kind": "chunk",
                    "chunk_ids": [chunks[0]["id"]],
                }
            ],
        }
        manifest["origin"] = {"kind": "continuation", "caller_run": str(caller_run)}
        manifest["request"]["specification"] += "\nPrior findings to reassess:\n" + json.dumps(
            [
                {
                    "signature": "caller-origin",
                    "tier": "high",
                    "structural": False,
                    "evidence": ["Caller record absent"],
                    "source_paths": ["alpha.py"],
                }
            ],
            sort_keys=True,
            ensure_ascii=True,
        )
        (out / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        stopped_results = out / "stopped-results.json"
        stopped_map = result_map | {"schema_version": 1}
        stopped_results.write_text(json.dumps(stopped_map), encoding="utf-8", newline="\n")
        original_check_clean_run = chunker._check_clean_run
        patch.setattr(chunker, "_check_clean_run", lambda *args, **kwargs: {"reason": "clean", "scores": [0]})
        stopped = chunker.check_stopped(out / "chunks.json", stopped_results)
        assert stopped["pending_chunks"] == []
        assert stopped["pending_interactions"] == []
        assert stopped["pending_groups"] == []
        assert stopped["pending_prior_signatures"] == ["caller-origin"]
        empty_ledger = _prior_ledger()
        empty_ledger["rounds"] = []
        (out / "runs" / chunks[0]["id"] / "loop-ledger.json").write_text(json.dumps(empty_ledger))
        assert chunker.check_stopped(out / "chunks.json", stopped_results)["pending_prior_signatures"] == [
            "caller-origin"
        ]
        patch.setattr(chunker, "_check_clean_run", original_check_clean_run)
        with pytest.raises(ValueError, match="prior-finding-not-resolved:caller-origin"):
            chunker.check(out / "chunks.json", results)
        current_ledger = _prior_ledger()
        current_ledger["rounds"][0]["findings"] = []
        (out / "runs" / chunks[0]["id"] / "loop-ledger.json").write_text(json.dumps(current_ledger))
        with pytest.raises(ValueError, match="prior-finding-not-resolved:caller-origin"):
            chunker.check(out / "chunks.json", results)
        current_ledger["rounds"][0]["findings"] = [
            {
                "signature": "caller-origin",
                "disposition": "verified-fixed",
                "tier": "high",
                "structural": False,
                "evidence": ["Caller record now present"],
            }
        ]
        child_run = out / "runs" / chunks[0]["id"]
        alpha = changed_repository / "alpha.py"
        current_alpha = alpha.read_bytes()
        try:
            alpha.write_text("VALUE = 'earlier reviewed alpha'\n", encoding="utf-8", newline="\n")
            stale_source, stale_diff = chunker._material(changed_repository, chunks[0]["scope_paths"])
        finally:
            alpha.write_bytes(current_alpha)
        (child_run / "source-1.json").write_bytes(stale_source)
        (child_run / "round-1.diff").write_bytes(stale_diff)
        child_evidence_path = child_run / "loop-evidence.json"
        child_evidence = json.loads(child_evidence_path.read_text(encoding="utf-8"))
        child_evidence["rounds"] = [{"source_path": "source-1.json", "supporting_source_path": None}]
        child_evidence_path.write_text(json.dumps(child_evidence), encoding="utf-8", newline="\n")
        current_source = json.loads((child_run / "current-source.json").read_text(encoding="utf-8"))
        current_ledger["current_snapshot"] = {
            "revision": current_source["revision"],
            "diff_digest": hashlib.sha256((child_run / "current.diff").read_bytes()).hexdigest(),
        }
        current_ledger["rounds"][0]["snapshot"] = {
            "revision": json.loads(stale_source)["revision"],
            "diff_digest": hashlib.sha256(stale_diff).hexdigest(),
        }
        (child_run / "loop-ledger.json").write_text(json.dumps(current_ledger), encoding="utf-8", newline="\n")
        patch.setattr(chunker, "_check_clean_run", lambda *args, **kwargs: {"reason": "plateau", "scores": [6]})
        assert chunker.check_stopped(out / "chunks.json", stopped_results)["pending_prior_signatures"] == [
            "caller-origin"
        ]
        (child_run / "source-1.json").write_bytes((child_run / "current-source.json").read_bytes())
        (child_run / "round-1.diff").write_bytes((child_run / "current.diff").read_bytes())
        current_ledger["current_snapshot"] = current_ledger["rounds"][0]["snapshot"] = {
            "revision": current_source["revision"],
            "diff_digest": hashlib.sha256((child_run / "current.diff").read_bytes()).hexdigest(),
        }
        (child_run / "loop-ledger.json").write_text(json.dumps(current_ledger), encoding="utf-8", newline="\n")
        assert chunker.check_stopped(out / "chunks.json", stopped_results)["pending_prior_signatures"] == []
        (child_run / "source-1.json").write_bytes(stale_source)
        assert chunker.check_stopped(out / "chunks.json", stopped_results)["pending_prior_signatures"] == [
            "caller-origin"
        ]
        (child_run / "source-1.json").write_bytes((child_run / "current-source.json").read_bytes())
        (child_run / "round-1.diff").write_bytes(stale_diff)
        assert chunker.check_stopped(out / "chunks.json", stopped_results)["pending_prior_signatures"] == [
            "caller-origin"
        ]
        (child_run / "round-1.diff").write_bytes((child_run / "current.diff").read_bytes())
        child_evidence["current_supporting_source_path"] = "current-supporting-source.json"
        child_evidence["rounds"][0]["supporting_source_path"] = "reviewed-supporting-source.json"
        child_evidence_path.write_text(json.dumps(child_evidence), encoding="utf-8", newline="\n")
        (child_run / "current-supporting-source.json").write_bytes((child_run / "current-source.json").read_bytes())
        (child_run / "reviewed-supporting-source.json").write_bytes(stale_source)
        assert chunker.check_stopped(out / "chunks.json", stopped_results)["pending_prior_signatures"] == [
            "caller-origin"
        ]
        child_evidence["current_supporting_source_path"] = None
        child_evidence["rounds"][0]["supporting_source_path"] = None
        child_evidence_path.write_text(json.dumps(child_evidence), encoding="utf-8", newline="\n")
        patch.setattr(chunker, "_check_clean_run", original_check_clean_run)
        assert chunker.check(out / "chunks.json", results) is not None
        current_ledger["rounds"][0]["findings"][0].update(tier="low", structural=True)
        assert chunker.validate_ledger(current_ledger) == []
        (out / "runs" / chunks[0]["id"] / "loop-ledger.json").write_text(json.dumps(current_ledger))
        with pytest.raises(ValueError, match="prior-finding-attribution-mismatch:caller-origin"):
            chunker.check(out / "chunks.json", results)
        patch.setattr(chunker, "_check_clean_run", lambda *args, **kwargs: {"reason": "clean", "scores": [0]})
        assert chunker.check_stopped(out / "chunks.json", stopped_results)["pending_prior_signatures"] == [
            "caller-origin"
        ]
        patch.setattr(chunker, "_check_clean_run", original_check_clean_run)
        manifest["prior_findings"]["coverage"][0]["source_paths"] = ["missing-validator.py"]
        manifest["request"]["specification"] = manifest["request"]["specification"].replace(
            '"source_paths": ["alpha.py"]', '"source_paths": ["missing-validator.py"]'
        )
        (out / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        with pytest.raises(ValueError, match="prior-source-coverage-incomplete:caller-origin"):
            chunker.check(out / "chunks.json", results)
        manifest["prior_findings"]["coverage"][0]["source_paths"] = ["alpha.py"]
        (out / "chunks.json").write_bytes(original_manifest)
        patch.setattr(chunker, "_check_results", original_check_results)
        child_result = out / "runs" / chunks[0]["id"] / "result.json"
        original_result = child_result.read_bytes()
        child_result.write_text(
            '{"status":"pass","metadata":{"adversarial_loop":{"reason":"clean","scores":[7],"rounds":[{"decision":"clean"}]}}}',
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(ValueError, match="chunk-result-score-invalid:chunk-001"):
            chunker.check(out / "chunks.json", results)
        child_result.write_bytes(original_result)
        child_result.write_text('{"status":"fail","metadata":{"adversarial_loop":{"reason":"clean"}}}')
        with pytest.raises(ValueError, match="chunk-result-not-clean:chunk-001"):
            chunker.check(out / "chunks.json", results)
        child_result.write_bytes(original_result)
        child = out / "runs" / chunks[0]["id"] / "loop-evidence.json"
        child_evidence = json.loads(child.read_text(encoding="utf-8"))
        for field in coordinator_request:
            child_evidence["request"] = coordinator_request | {field: f"Substituted {field}"}
            child.write_text(json.dumps(child_evidence), encoding="utf-8", newline="\n")
            with pytest.raises(ValueError, match="chunk-result-request-mismatch:chunk-001"):
                chunker.check(out / "chunks.json", results)
        child_evidence["request"] = coordinator_request
        child.write_text(json.dumps(child_evidence), encoding="utf-8", newline="\n")
        for field in ("goal", "done_when"):
            request[field] = f"Substituted {field}"
            (run / "loop-evidence.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "repository": changed_repository.resolve().as_posix(),
                        "scope_paths": paths,
                        "current_source_path": "current-source.json",
                        "request": request,
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )
            with pytest.raises(ValueError, match="chunk-result-request-mismatch:chunk-001\\+chunk-002"):
                chunker.check(out / "chunks.json", results)
            request[field] = coordinator_request[field]
        request["specification"] = "Substituted contract\nInteraction assessment:\n" + json.dumps(
            decisions, sort_keys=True, ensure_ascii=True
        )
        (run / "loop-evidence.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "repository": changed_repository.resolve().as_posix(),
                    "scope_paths": paths,
                    "current_source_path": "current-source.json",
                    "request": request,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(ValueError, match="chunk-result-request-mismatch:chunk-001\\+chunk-002"):
            chunker.check(out / "chunks.json", results)
        request["specification"] = (
            coordinator_request["specification"]
            + "\nInteraction assessment:\n"
            + json.dumps(decisions, sort_keys=True, ensure_ascii=True)
        )
        (run / "loop-evidence.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "repository": changed_repository.resolve().as_posix(),
                    "scope_paths": paths,
                    "current_source_path": "current-source.json",
                    "request": request,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        child_evidence["schema_version"] = 1
        child_evidence.pop("request")
        child.write_text(json.dumps(child_evidence), encoding="utf-8", newline="\n")
        with pytest.raises(ValueError, match="chunk-result-request-missing:chunk-001"):
            chunker.check(out / "chunks.json", results)
        child_evidence["schema_version"] = 2
        child_evidence["request"] = coordinator_request
        child.write_text(json.dumps(child_evidence), encoding="utf-8", newline="\n")
        (run / "current-source.json").write_bytes(b"stale")
        with pytest.raises(ValueError, match="chunk-result-source-mismatch:chunk-001\\+chunk-002"):
            chunker.check(out / "chunks.json", results)
        (run / "current-source.json").write_bytes(source)
        request["specification"] = coordinator_request["specification"] + " without interaction decisions"
        (run / "loop-evidence.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "repository": changed_repository.resolve().as_posix(),
                    "scope_paths": paths,
                    "current_source_path": "current-source.json",
                    "request": request,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(ValueError, match="chunk-interaction-assessment-missing"):
            chunker.check(out / "chunks.json", results)
        request["specification"] = (
            coordinator_request["specification"]
            + "\nInteraction assessment:\n"
            + json.dumps(decisions[:-1], sort_keys=True, ensure_ascii=True)
        )
        (run / "loop-evidence.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "repository": changed_repository.resolve().as_posix(),
                    "scope_paths": paths,
                    "current_source_path": "current-source.json",
                    "request": request,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(ValueError, match="chunk-interaction-assessment-missing"):
            chunker.check(out / "chunks.json", results)
        result_map["interactions"][0]["chunk_ids"] = [chunks[0]["id"]]
        results.write_text(json.dumps(result_map), encoding="utf-8", newline="\n")
        with pytest.raises(ValueError, match="chunk-interaction-coverage-invalid"):
            chunker.check(out / "chunks.json", results)


@pytest.mark.packaging
def test_all_scope_mode_covers_unchanged_plugin_files(changed_repository: Path, tmp_path: Path) -> None:
    """A whole plugin review must retain unchanged tracked source too."""
    plugin = changed_repository / "plugin"
    plugin.mkdir()
    (plugin / "entry.py").write_text("VALUE = 'stable'\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "plugin/entry.py"], cwd=changed_repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "plugin"],
        cwd=changed_repository,
        check=True,
        capture_output=True,
    )
    out = tmp_path / "plugin-review"

    planned = _run(
        "plan",
        "--repository",
        str(changed_repository),
        "--out",
        str(out),
        "--scope-path",
        "plugin",
        "--scope-mode",
        "all",
        "--budget-bytes",
        "900",
        *REQUEST_ARGS,
    )

    assert planned.returncode == 0, planned.stderr
    manifest = json.loads((out / "chunks.json").read_text(encoding="utf-8"))
    assert manifest["inventory"] == ["plugin/entry.py"]
    chunk = manifest["chunks"][0]
    assert (out / chunk["diff_path"]).read_bytes() == b""
    assert chunk["diff_sha256"] == hashlib.sha256(b"").hexdigest()
    assert b"VALUE = 'stable'" in (out / chunk["source_path"]).read_bytes()
    assert _run("check", "--manifest", str(out / "chunks.json")).returncode == 0


@pytest.mark.packaging
def test_plan_preserves_trailing_space_in_repository_root(changed_repository: Path, tmp_path: Path) -> None:
    """Use the real root name when planning from a root or its subdirectory."""
    repository = changed_repository.rename(tmp_path / "repository ")
    physical_repository = repository.resolve(strict=True)
    out = tmp_path / "review"

    planned = _run("plan", "--repository", str(repository), "--out", str(out), "--budget-bytes", "900", *REQUEST_ARGS)

    assert planned.returncode == 0, planned.stderr
    assert json.loads((out / "chunks.json").read_text(encoding="utf-8"))["repository"] == physical_repository.as_posix()
    assert _run("check", "--manifest", str(out / "chunks.json")).returncode == 0

    nested = physical_repository / "nested"
    nested.mkdir()
    nested_out = tmp_path / "nested-review"
    nested_plan = _run(
        "plan", "--repository", str(nested), "--out", str(nested_out), "--budget-bytes", "900", *REQUEST_ARGS
    )
    assert nested_plan.returncode == 0, nested_plan.stderr
    assert (
        json.loads((nested_out / "chunks.json").read_text(encoding="utf-8"))["repository"]
        == physical_repository.as_posix()
    )
    assert _run("check", "--manifest", str(nested_out / "chunks.json")).returncode == 0
