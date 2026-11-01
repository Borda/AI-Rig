"""Bind displayed review commits to retained source rather than the current checkout."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _artifact_files import read_json
from _platform import FILE_SYMLINKS_AVAILABLE
from test_gate_source import _git, _initialize_repository, _python_command, _run_lint_gate
from test_remediation_finalize import _load_helper
from test_review_batches import _batch_inputs, _prepare_batches_in_process
from test_review_completion_gate import FINAL_HANDOFF, VALIDATE_SHARED, _assessed_pr, _module

SHA40 = "a" * 40
SHA64 = "a" * 64


@pytest.fixture
def assessed_run(tmp_path: Path) -> Path:
    """Complete trivial PR review run built through the real render and validation boundaries."""
    return _assessed_pr.__wrapped__(tmp_path)


def _finalizer() -> Any:
    """Fresh copy of the standalone final-handoff module."""
    return _module(FINAL_HANDOFF)


def _shared_validator() -> Any:
    """Fresh copy of the shared artifact validator module."""
    return _module(VALIDATE_SHARED)


def _load_review(run: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the run's result and final handoff documents."""
    return read_json(run / "result.json"), read_json(run / "final-handoff.json")


def _remove_commit_row(handoff: dict[str, Any]) -> dict[str, Any]:
    """Drop the snapshot's Commit row and its source record, keeping source coverage consistent; return the row."""
    rows = handoff["tables"][0]["rows"]
    removed = next(row for row in rows if row["cells"][0] == "Commit")
    rows.remove(removed)
    handoff["source_records"] = [
        record for record in handoff["source_records"] if record["id"] not in removed["source_ids"]
    ]
    count = len(handoff["source_records"])
    handoff["source_coverage"].update(source_records_total=count, represented_source_records_total=count)
    return removed


def _write_files(root: Path, files: dict[str, str]) -> None:
    """Write each named text file below ``root``."""
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")


def _keep_routing(run: Path) -> None:
    """Leave the PR routing receipt in place."""


def _drop_routing(run: Path) -> None:
    """Remove the PR routing receipt so only the local checkout receipt names the head."""
    (run / "pr-routing.json").unlink()


def _leave_clean(repository: Path) -> None:
    """Leave the committed source untouched."""


def _dirty_tracked_file(repository: Path) -> None:
    """Modify a tracked file so the source is no longer clean."""
    (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")


def _keep_gates(run: Path) -> None:
    """Leave the recorded gate observations untouched."""


def _strip_observations(run: Path) -> None:
    """Remove the observed source from every recorded gate check."""
    gates = read_json(run / "gates.json")
    for check in gates["checks"]:
        check.pop("source", None)
    (run / "gates.json").write_text(json.dumps(gates), encoding="utf-8")


def _write_conflicting_sidecar(run: Path) -> None:
    """Add a reviewed-commit sidecar naming a different commit than the observed source."""
    (run / "reviewed-commit.txt").write_text("0" * 40 + "\n", encoding="utf-8")


def _drop_commit_row_only(result: dict[str, Any], rows: list[Any], row: dict[str, Any]) -> None:
    """Remove the header row while metadata still names the commit."""
    rows.remove(row)


def _drop_metadata_only(result: dict[str, Any], rows: list[Any], row: dict[str, Any]) -> None:
    """Remove the metadata commit while the header row remains."""
    result["metadata"].pop("reviewed_commit_sha")


def _duplicate_commit_row(result: dict[str, Any], rows: list[Any], row: dict[str, Any]) -> None:
    """Insert a second Commit row in the header."""
    rows.insert(-1, row)


def _change_metadata_commit(result: dict[str, Any], rows: list[Any], row: dict[str, Any]) -> None:
    """Point the metadata at a different commit than the header row."""
    result["metadata"]["reviewed_commit_sha"] = "c" * 40


class TestCommitFinalization:
    """Finalizing a commit review derives its displayed commit from observed gate source."""

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("command", "returncode", "in_worktree"),
        [
            pytest.param("pass", 0, True, id="explicit-worktree"),
            pytest.param("pass", 0, False, id="current-directory"),
            pytest.param("raise SystemExit(1)", 1, True, id="failed-command"),
        ],
    )
    def test_uses_observed_gate_source(
        self, tmp_path: Path, assessed_run: Path, command: str, returncode: int, in_worktree: bool
    ) -> None:
        """A commit review is finalized from the commit its lint gate observed, without receipts or batches.

        A real detached repository runs the lint gate in an explicit worktree, the current directory, or with a failing
        command; the derived handoff must show the observed head either way and still validate.
        """
        run = assessed_run
        repository = tmp_path / "committed-source"
        repository.mkdir()
        head = _initialize_repository(repository)
        _git(repository, "checkout", "--detach", head)
        checked = _run_lint_gate(
            repository, run, head, _python_command(command), worktree=repository if in_worktree else None
        )
        assert checked.returncode == returncode, checked.stderr
        assert not (run / "reviewed-commit.txt").exists()
        assert not (run / "batch-inventory.json").exists()
        result, handoff = _load_review(run)
        metadata = result["metadata"]
        metadata["scope"] = "commit"
        metadata.pop("reviewed_commit_sha")
        snapshot = handoff["tables"][0]
        snapshot["heading"] = "Review Snapshot"
        _remove_commit_row(handoff)
        snapshot["rows"][0]["cells"] = ["Scope", "commit"]
        snapshot["rows"][1]["cells"] = ["Revision", f"diff sha256:{metadata['review_input_sha256']}"]
        snapshot["rows"][2]["cells"] = ["CI", "unavailable"]

        derived = _load_helper().derive_handoff(run, metadata, handoff, result["confidence"], result["artifact_path"])

        assert metadata["reviewed_commit_sha"] == head
        assert snapshot["rows"][-2]["cells"] == ["Commit", head]
        source = next(record for record in derived["source_records"] if record["id"] == "reviewed-commit")
        assert source["evidence"] == "gates.json"
        assert f"Reviewed commit: {head}" in (run / "review-notes.md").read_text(encoding="utf-8")
        assert f"| Commit | {head} |" in _finalizer().render_handoff(derived, report_root=run)
        _shared_validator()._validate_code_review_final_handoff(
            result, derived, candidate=True, fresh_candidate=True, out_dir=run
        )

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("wrong_expected", "dirty", "after_gate", "returncode"),
        [
            pytest.param(True, _leave_clean, _keep_gates, 1, id="wrong-expected"),
            pytest.param(False, _dirty_tracked_file, _keep_gates, 1, id="dirty-source"),
            pytest.param(False, _leave_clean, _strip_observations, 0, id="no-observations"),
            pytest.param(False, _leave_clean, _write_conflicting_sidecar, 0, id="conflicting-sidecar"),
        ],
    )
    def test_identity_rejects_unverified_gate_requests(
        self,
        tmp_path: Path,
        wrong_expected: bool,
        dirty: Callable[[Path], None],
        after_gate: Callable[[Path], None],
        returncode: int,
    ) -> None:
        """An expected SHA alone never stands in for observed clean committed source.

        A real repository is gated with a wrong expectation, dirty source, stripped observations or a conflicting
        sidecar; deriving the reviewed commit afterwards must be refused in every case.
        """
        repository = tmp_path / "repository"
        repository.mkdir()
        head = _initialize_repository(repository)
        run = tmp_path / "run"
        dirty(repository)
        checked = _run_lint_gate(
            repository, run, "0" * 40 if wrong_expected else head, _python_command("pass"), repository
        )
        assert checked.returncode == returncode, checked.stderr
        after_gate(run)

        with pytest.raises(ValueError, match="review-commit-"):
            _finalizer().review_commit_sha(run, "commit")


class TestReviewCommitSha:
    """The finalizer resolves the reviewed commit from retained source, never from the invoking Git HEAD."""

    @pytest.mark.parametrize(
        ("scope", "filename", "payload", "expected"),
        [
            pytest.param("pr", "pr-routing.json", {"head_oid": SHA40}, SHA40, id="pr-head"),
            pytest.param(
                "working-tree", "local-source/source-snapshot.json", {"revision": SHA40}, SHA40, id="dirty-head-base"
            ),
            pytest.param("path", "local-source/source-snapshot.json", {"revision": SHA40}, SHA40, id="path-head-base"),
            pytest.param("commit", "reviewed-commit.txt", SHA40 + "\n", SHA40, id="detached-range-tip"),
            pytest.param(
                "commit",
                "batch-inventory.json",
                {"source_snapshot": {"revision": SHA64}},
                SHA64,
                id="batched-sha256-tip",
            ),
        ],
    )
    def test_resolves_from_retained_source(
        self, tmp_path: Path, scope: str, filename: str, payload: object, expected: str
    ) -> None:
        """Every review route resolves its commit from retained files without reading the invoking Git HEAD.

        PR, working-tree, path, detached-range and batched routes each store the revision in a different artifact.
        """
        source = tmp_path / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")

        assert _finalizer().review_commit_sha(tmp_path, scope) == expected

    @pytest.mark.parametrize(
        "files",
        [
            pytest.param({}, id="missing"),
            pytest.param({"reviewed-commit.txt": "a" * 12}, id="short"),
            pytest.param({"reviewed-commit.txt": "A" * 40}, id="uppercase"),
            pytest.param(
                {
                    "reviewed-commit.txt": SHA40,
                    "batch-inventory.json": json.dumps({"source_snapshot": {"revision": "b" * 40}}),
                },
                id="conflict",
            ),
        ],
    )
    def test_rejects_unbound_or_conflicting_identity(self, tmp_path: Path, files: dict[str, str]) -> None:
        """A provenance gap is never filled with an invented, abbreviated or contradictory commit.

        The retained sidecars are missing, abbreviated, upper-case, or name two different commits.
        """
        _write_files(tmp_path, files)

        with pytest.raises(ValueError, match="review-commit-"):
            _finalizer().review_commit_sha(tmp_path, "commit")

    @pytest.mark.integration
    def test_batched_review_uses_producer_source_inventory(self, tmp_path: Path) -> None:
        """Source batching retains its own reviewed SHA without requiring a single-wave sidecar.

        After real batch preparation only the producer inventory exists; its revision is what the finalizer reports.
        """
        run = _batch_inputs(tmp_path)
        _prepare_batches_in_process(run)
        inventory = read_json(run / "batch-inventory.json")
        assert not (run / "reviewed-commit.txt").exists()

        assert _finalizer().review_commit_sha(run, "working-tree") == inventory["source_snapshot"]["revision"]


class TestDisplayedCommit:
    """The header Commit row, the metadata and the full report must name the same retained commit."""

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("receipt", "prepare"),
        [
            pytest.param("pr-routing.json", _keep_routing, id="routing-receipt"),
            pytest.param("local-checkout.json", _drop_routing, id="checkout-receipt"),
        ],
    )
    def test_finalizer_derives_commit_in_header_and_full_report(
        self, assessed_run: Path, receipt: str, prepare: Callable[[Path], None]
    ) -> None:
        """The displayed commit is derived from the PR receipt and the full report is retained.

        With the Commit row and metadata removed, derivation must restore both from the routing or checkout receipt,
        keep the original report text and render the commit in the header.
        """
        run = assessed_run
        result, handoff = _load_review(run)
        original = (run / "review-notes.md").read_text(encoding="utf-8")
        metadata = result["metadata"]
        head = read_json(run / "pr-routing.json")["head_oid"]
        prepare(run)
        metadata.pop("reviewed_commit_sha")
        _remove_commit_row(handoff)

        derived = _load_helper().derive_handoff(run, metadata, handoff, result["confidence"], result["artifact_path"])

        snapshot = derived["tables"][0]
        row = next(row for row in snapshot["rows"] if row["cells"][0] == "Commit")
        source = next(record for record in derived["source_records"] if record["id"] == row["source_ids"][0])
        notes = (run / "review-notes.md").read_text(encoding="utf-8")
        assert metadata["reviewed_commit_sha"] == head
        assert row["cells"][1] == head
        assert snapshot["rows"][-2] == row
        assert source["evidence"] == receipt
        assert (run / source["evidence"]).is_file()
        assert original in notes
        assert f"Reviewed commit: {head}" in notes
        assert f"| Commit | {head} |" in _finalizer().render_handoff(derived, report_root=run)
        _shared_validator()._validate_code_review_final_handoff(
            result, derived, candidate=True, fresh_candidate=True, out_dir=run
        )

    @pytest.mark.integration
    def test_tampered_header_commit_is_rejected(self, assessed_run: Path) -> None:
        """A derived handoff whose header commit is changed afterwards fails validation.

        The commit is derived from the routing receipt, then the Commit row is overwritten with a different SHA.
        """
        result, handoff = _load_review(assessed_run)
        _remove_commit_row(handoff)
        result["metadata"].pop("reviewed_commit_sha")
        derived = _load_helper().derive_handoff(
            assessed_run, result["metadata"], handoff, result["confidence"], result["artifact_path"]
        )
        row = next(row for row in derived["tables"][0]["rows"] if row["cells"][0] == "Commit")
        row["cells"][1] = "c" * 40

        with pytest.raises(SystemExit, match="review-commit"):
            _shared_validator()._validate_code_review_final_handoff(
                result, derived, candidate=True, fresh_candidate=True, out_dir=assessed_run
            )

    def test_historical_result_without_commit_remains_readable(self, assessed_run: Path) -> None:
        """A historical canonical result lacking a commit stays readable without being rewritten.

        With both the metadata commit and the header row removed, validation of a non-fresh candidate must pass.
        """
        result, handoff = _load_review(assessed_run)
        result["metadata"].pop("reviewed_commit_sha")
        handoff["tables"][0]["rows"] = [row for row in handoff["tables"][0]["rows"] if row["cells"][0] != "Commit"]

        _shared_validator()._validate_code_review_final_handoff(result, handoff, candidate=True, out_dir=assessed_run)

    def test_new_review_requires_commit(self, assessed_run: Path) -> None:
        """Traceability is enforced when a new review is promoted.

        The same commit-free result and handoff that stay readable historically must be rejected as a fresh candidate.
        """
        result, handoff = _load_review(assessed_run)
        result["metadata"].pop("reviewed_commit_sha")
        handoff["tables"][0]["rows"] = [row for row in handoff["tables"][0]["rows"] if row["cells"][0] != "Commit"]

        with pytest.raises(SystemExit, match="review-commit-missing"):
            _shared_validator()._validate_code_review_final_handoff(
                result, handoff, candidate=True, fresh_candidate=True, out_dir=assessed_run
            )

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_drop_commit_row_only, id="row-removed"),
            pytest.param(_drop_metadata_only, id="metadata-removed"),
            pytest.param(_duplicate_commit_row, id="duplicate-row"),
            pytest.param(_change_metadata_commit, id="wrong-metadata"),
        ],
    )
    def test_metadata_and_header_cannot_diverge(
        self, assessed_run: Path, damage: Callable[[dict[str, Any], list[Any], dict[str, Any]], None]
    ) -> None:
        """Every partial or duplicated tracing claim is rejected instead of rendering a plausible SHA.

        Removing only the header row, only the metadata, duplicating the row or disagreeing on the SHA must all fail.
        """
        result, handoff = _load_review(assessed_run)
        rows = handoff["tables"][0]["rows"]
        row = next(row for row in rows if row["cells"][0] == "Commit")
        damage(result, rows, row)

        with pytest.raises(SystemExit, match=r"(?:review-commit|snapshot.*fields-mismatch)"):
            _shared_validator()._validate_code_review_final_handoff(
                result, handoff, candidate=True, fresh_candidate=True, out_dir=assessed_run
            )

    @pytest.mark.integration
    @pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks")
    def test_report_derivation_rejects_symlink_before_writing(self, tmp_path: Path, assessed_run: Path) -> None:
        """A rejected artifact must not change an external file through its report path.

        The review notes become a symlink to an outside file; derivation must refuse and leave that file untouched.
        """
        run = assessed_run
        result, handoff = _load_review(run)
        report = run / "review-notes.md"
        target = tmp_path / "external-report.md"
        target.write_bytes(report.read_bytes())
        original = target.read_bytes()
        report.unlink()
        report.symlink_to(target)
        helper = _load_helper()

        with pytest.raises(helper.DeriveError, match="review-commit-report-invalid"):
            helper.derive_handoff(run, result["metadata"], handoff, result["confidence"], result["artifact_path"])

        assert target.read_bytes() == original
