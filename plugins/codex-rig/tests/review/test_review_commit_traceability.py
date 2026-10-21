"""Bind displayed review commits to retained source rather than the current checkout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _platform import FILE_SYMLINKS_AVAILABLE
from test_final_handoff import _load_finalizer, _load_shared_validator
from test_remediation_finalize import _load_helper
from test_review_batches import _batch_inputs, _prepare_batches_in_process
from test_review_completion_gate import _assessed_pr


@pytest.mark.parametrize(
    ("scope", "filename", "payload"),
    [
        pytest.param("pr", "pr-routing.json", {"head_oid": "a" * 40}, id="pr-head"),
        pytest.param("working-tree", "local-source/source-snapshot.json", {"revision": "a" * 40}, id="dirty-head-base"),
        pytest.param("path", "local-source/source-snapshot.json", {"revision": "a" * 40}, id="path-head-base"),
        pytest.param("commit", "reviewed-commit.txt", "a" * 40, id="detached-range-tip"),
        pytest.param(
            "commit", "batch-inventory.json", {"source_snapshot": {"revision": "a" * 64}}, id="batched-sha256-tip"
        ),
    ],
)
def test_review_commit_uses_retained_source(tmp_path: Path, scope: str, filename: str, payload: object) -> None:
    """Resolve every review route without reading or depending on the invoking Git HEAD."""
    source = tmp_path / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(payload + "\n" if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    assert _load_finalizer().review_commit_sha(tmp_path, scope) == "a" * (
        64 if filename == "batch-inventory.json" else 40
    )


@pytest.mark.parametrize("damage", ["missing", "short", "uppercase", "conflict"])
def test_review_commit_rejects_unbound_or_conflicting_identity(tmp_path: Path, damage: str) -> None:
    """Never fill a provenance gap using an invented, abbreviated or contradictory commit."""
    if damage != "missing":
        value = "a" * 12 if damage == "short" else "A" * 40 if damage == "uppercase" else "a" * 40
        (tmp_path / "reviewed-commit.txt").write_text(value, encoding="utf-8")
    if damage == "conflict":
        (tmp_path / "batch-inventory.json").write_text(
            json.dumps({"source_snapshot": {"revision": "b" * 40}}), encoding="utf-8"
        )
    with pytest.raises(ValueError, match="review-commit-"):
        _load_finalizer().review_commit_sha(tmp_path, "commit")


@pytest.mark.integration
@pytest.mark.parametrize(
    "receipt",
    [pytest.param("pr-routing.json", id="routing-receipt"), pytest.param("local-checkout.json", id="checkout-receipt")],
)
def test_review_finalizer_binds_commit_in_header_and_full_report(tmp_path: Path, receipt: str) -> None:
    """Derive the displayed commit from the PR receipt, retain report content and reject tampering."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    original = (run / "review-notes.md").read_text(encoding="utf-8")
    metadata = result["metadata"]
    head = json.loads((run / "pr-routing.json").read_text(encoding="utf-8"))["head_oid"]
    if receipt == "local-checkout.json":
        (run / "pr-routing.json").unlink()
    metadata.pop("reviewed_commit_sha")
    removed = next(row for row in handoff["tables"][0]["rows"] if row["cells"][0] == "Commit")
    handoff["tables"][0]["rows"].remove(removed)
    handoff["source_records"] = [
        record for record in handoff["source_records"] if record["id"] not in removed["source_ids"]
    ]
    count = len(handoff["source_records"])
    handoff["source_coverage"].update(source_records_total=count, represented_source_records_total=count)

    helper = _load_helper()
    derived = helper.derive_handoff(run, metadata, handoff, result["confidence"], result["artifact_path"])
    snapshot = derived["tables"][0]
    assert metadata["reviewed_commit_sha"] == head
    row = next(row for row in snapshot["rows"] if row["cells"][0] == "Commit")
    assert row["cells"][1] == head
    assert snapshot["rows"][-2] == row
    source = next(record for record in derived["source_records"] if record["id"] == row["source_ids"][0])
    assert source["evidence"] == receipt
    assert (run / source["evidence"]).is_file()
    assert original in (run / "review-notes.md").read_text(encoding="utf-8")
    assert f"Reviewed commit: {head}" in (run / "review-notes.md").read_text(encoding="utf-8")
    rendered = _load_finalizer().render_handoff(derived, report_root=run)
    assert f"| Commit | {head} |" in rendered
    validator = _load_shared_validator()
    validator._validate_code_review_final_handoff(result, derived, candidate=True, fresh_candidate=True, out_dir=run)
    row["cells"][1] = "c" * 40
    with pytest.raises(SystemExit, match="review-commit"):
        validator._validate_code_review_final_handoff(
            result, derived, candidate=True, fresh_candidate=True, out_dir=run
        )


def test_new_review_requires_commit_but_historical_result_remains_readable(tmp_path: Path) -> None:
    """Enforce traceability during promotion without rewriting a historical canonical result."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    validator = _load_shared_validator()
    result["metadata"].pop("reviewed_commit_sha")
    handoff["tables"][0]["rows"] = [row for row in handoff["tables"][0]["rows"] if row["cells"][0] != "Commit"]
    validator._validate_code_review_final_handoff(result, handoff, candidate=True, out_dir=run)
    with pytest.raises(SystemExit, match="review-commit-missing"):
        validator._validate_code_review_final_handoff(
            result, handoff, candidate=True, fresh_candidate=True, out_dir=run
        )


@pytest.mark.parametrize("damage", ["metadata-only", "row-only", "duplicate-row", "wrong-metadata"])
def test_commit_metadata_and_header_cannot_diverge(tmp_path: Path, damage: str) -> None:
    """Reject every partial or duplicated tracing claim instead of silently rendering a plausible SHA."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    rows = handoff["tables"][0]["rows"]
    row = next(row for row in rows if row["cells"][0] == "Commit")
    if damage == "metadata-only":
        rows.remove(row)
    elif damage == "row-only":
        result["metadata"].pop("reviewed_commit_sha")
    elif damage == "duplicate-row":
        rows.insert(-1, row)
    else:
        result["metadata"]["reviewed_commit_sha"] = "c" * 40
    with pytest.raises(SystemExit, match=r"(?:review-commit|snapshot.*fields-mismatch)"):
        _load_shared_validator()._validate_code_review_final_handoff(
            result, handoff, candidate=True, fresh_candidate=True, out_dir=run
        )


@pytest.mark.integration
def test_batched_review_uses_producer_source_inventory(tmp_path: Path) -> None:
    """Source batching retains its own reviewed SHA without requiring a single-wave sidecar."""
    run = _batch_inputs(tmp_path)
    _prepare_batches_in_process(run)
    inventory = json.loads((run / "batch-inventory.json").read_text(encoding="utf-8"))
    assert not (run / "reviewed-commit.txt").exists()
    assert _load_finalizer().review_commit_sha(run, "working-tree") == inventory["source_snapshot"]["revision"]


@pytest.mark.integration
@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks")
def test_report_derivation_rejects_symlink_before_writing(tmp_path: Path) -> None:
    """A rejected artifact must not change an external file through its report path."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    report = run / "review-notes.md"
    target = tmp_path / "external-report.md"
    target.write_bytes(report.read_bytes())
    original = target.read_bytes()
    report.unlink()
    report.symlink_to(target)
    helper = _load_helper()
    try:
        with pytest.raises(helper.DeriveError, match="review-commit-report-invalid"):
            helper.derive_handoff(run, result["metadata"], handoff, result["confidence"], result["artifact_path"])
    finally:
        assert target.read_bytes() == original
