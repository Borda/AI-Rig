"""Regression checks for derived remediation artifacts, one-pass validation, and one-command finalization."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module
from _platform import FILE_SYMLINKS_AVAILABLE
from _skill_text import read_with_references
from test_audit_acceptance import (
    _gate_template,  # noqa: F401
    _validator,  # noqa: F401
    audit_run,  # noqa: F401
)
from test_finding_presentation import VALIDATOR, _write_pr_run_artifacts, _write_remediation_candidate

# Importing the fixture function registers the shared `assessed_pr` fixture in this module.
from test_review_completion_gate import (
    FINDER,
    _assessed_pr,  # noqa: F401
    _finalize_parent_fallback,
)
from test_review_finding_identity import _load_validator, _metadata, _notes, _result

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DERIVED_HANDOFF_FIELDS = ("verification", "confidence", "artifacts", "tables", "source_records", "source_coverage")


def _load_helper() -> ModuleType:
    """Load the remediation finalize helper by file path, as the skill invokes it."""
    return load_module("codex_rig_remediation_finalize", PLUGIN_ROOT / "shared" / "remediation_finalize.py")


HELPER = _load_helper()


def _stale_metadata(result: dict[str, object]) -> dict[str, object]:
    """Return the run's metadata with every derivable copy removed or corrupted, as a hand draft would drift."""
    metadata = copy.deepcopy(result["metadata"])
    metadata.pop("final_handoff")
    metadata["resolution_scope"].update(deferred_indexes=[9], presentation_version=1)
    for key in ("bucket_plan_sha256", "groups_total", "verifier_groups", "work_buckets", "parallel_approval_status"):
        metadata["resolution_workplan"].pop(key)
    for item in metadata["final_resolution_table"]["items"]:
        item["item_name"] = item["item_name"].upper()
    return metadata


def _draft_handoff(run: Path) -> dict[str, object]:
    """Return the run's handoff with every derivable field removed."""
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    for field in DERIVED_HANDOFF_FIELDS:
        handoff.pop(field)
    return handoff


_SELECTION_MARKS = [("CLOSED", False), ("OMITTED", True), ("CHOSEN", True)]


def _marked_item(original: dict[str, object], identity: str, selectable: bool) -> dict[str, object]:
    """Copy a resolution item under a new identity, selectability and source ids, as implemented."""
    item = copy.deepcopy(original)
    item.update(
        input_item_id=identity, item_name=identity, selectable=selectable, resolved_how="Implemented: Added the guard."
    )
    for source_index, source in enumerate(item["sources"]):
        source["source_id"] = f"{identity}-{source_index}"
    return item


def _without_selected(row: dict[str, object]) -> dict[str, object]:
    """Return a handoff row without its ``selected`` mark."""
    return {key: value for key, value in row.items() if key != "selected"}


def _set_resolved_how(metadata: dict[str, object], text: str) -> None:
    """Give every resolution item the same ``resolved_how`` text."""
    for item in metadata["final_resolution_table"]["items"]:
        item["resolved_how"] = text


def _clear_workplan(workplan: dict[str, object]) -> None:
    """Turn a workplan into the empty-selection form: no buckets, zero counts, no plan paths or digest."""
    workplan["work_buckets"] = []
    for key in (
        "groups_total",
        "parent_owned_groups",
        "specialist_owned_groups",
        "verifier_groups",
        "unassigned_selected_items",
    ):
        workplan[key] = 0
    for key in ("workplan_path", "bucket_plan_path", "parallel_approval_path", "bucket_plan_sha256"):
        workplan.pop(key, None)


class TestHandoffDerivation:
    """Protect the derived handoff: checkout location, selection marks and presentation versions."""

    def test_reports_exact_delivered_topic_commits(self, valid_run: tuple[Path, dict[str, object]]) -> None:
        """Use the verified lifecycle's ordered commits in the ordinary handoff producer."""
        run, result = valid_run
        metadata = result["metadata"]
        commits = ["1" * 40, "2" * 40]
        lifecycle = run / "production-lifecycle.json"
        lifecycle.write_text(
            json.dumps(
                {"schema_version": 3, "status": "completed", "commit_mode": "grouped", "integrated_commits": commits}
            ),
            encoding="utf-8",
        )
        (run / "work-bucket-plan.json").write_text('{"schema_version": 3}', encoding="utf-8")
        metadata["resolution_workplan"]["production_lifecycle"] = {
            "path": lifecycle.name,
            "status": "completed",
            "sha256": hashlib.sha256(lifecycle.read_bytes()).hexdigest(),
        }
        draft = _draft_handoff(run)
        draft["commit_disposition"] = {"status": "committed", "reason": "Wrong commits", "evidence": "gates.json"}

        handoff = HELPER.derive_handoff(run, metadata, draft, result["confidence"], result["artifact_path"])

        assert handoff["commit_disposition"] == {
            "status": "committed",
            "reason": f"Delivered topic commits in order ({', '.join(commits)}).",
            "evidence": lifecycle.name,
        }

    @pytest.mark.parametrize(
        ("receipt_name", "worktree"),
        [
            pytest.param("remediation-branch.json", "/workspace/project fix", id="original-posix"),
            pytest.param("remediation-branch-recovered.json", "C:/workspace/project fix", id="recovered-windows"),
        ],
    )
    def test_locates_recorded_checkout(
        self, valid_run: tuple[Path, dict[str, object]], receipt_name: str, worktree: str
    ) -> None:
        """Show the receipt's branch and exact checkout even when finalization runs elsewhere.

        A stale original receipt and a stale draft artifact name an old checkout while the selected receipt (original
        POSIX or recovered Windows) names the live one; derivation must replace the stale entry, stay idempotent, and
        render the live checkout link only.
        """
        run, result = valid_run
        result["metadata"]["resolution_scope"]["presentation_version"] = 4
        receipt = {"schema_version": 2, "status": "prepared", "branch": "fix/selection", "worktree": worktree}
        (run / "remediation-branch.json").write_text(
            json.dumps({**receipt, "branch": "old-branch", "worktree": "/old/checkout"}), encoding="utf-8"
        )
        (run / receipt_name).write_text(json.dumps(receipt), encoding="utf-8")
        handoff = _draft_handoff(run)
        handoff["artifacts"] = [{"label": "Source checkout: stale", "path": "/old/checkout"}]

        handoff = HELPER.derive_handoff(run, result["metadata"], handoff, result["confidence"], result["artifact_path"])
        repeated = HELPER.derive_handoff(
            run, result["metadata"], copy.deepcopy(handoff), result["confidence"], result["artifact_path"]
        )
        rendered = HELPER.final_handoff.render_handoff(handoff)

        assert repeated == handoff
        assert [item for item in handoff["artifacts"] if item["label"].startswith("Source checkout:")] == [
            {"label": f"Source checkout: {worktree} (branch fix/selection)", "path": worktree}
        ]
        assert f"[Source checkout: {worktree} (branch fix/selection)](<{worktree}>)" in rendered
        assert "/old/checkout" not in rendered

    @pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="file symlink capability unavailable")
    def test_rejects_dangling_recovered_receipt(self, valid_run: tuple[Path, dict[str, object]]) -> None:
        """A broken recovery link must not resurrect the old source checkout.

        The recovered receipt is a symlink to a missing file while a valid original receipt exists; derivation must fail
        with an unreadable-json error instead of falling back to the original checkout.
        """
        run, result = valid_run
        receipt = {"schema_version": 2, "status": "prepared", "branch": "fix/selection", "worktree": "/old/checkout"}
        (run / "remediation-branch.json").write_text(json.dumps(receipt), encoding="utf-8")
        (run / "remediation-branch-recovered.json").symlink_to(run / "missing-receipt.json")

        with pytest.raises(HELPER.DeriveError, match=r"unreadable-json:remediation-branch-recovered\.json"):
            HELPER.derive_handoff(
                run, result["metadata"], _draft_handoff(run), result["confidence"], result["artifact_path"]
            )

    @pytest.mark.parametrize(
        ("change", "raw"),
        [
            pytest.param({"schema_version": 1}, None, id="legacy"),
            pytest.param({"worktree": "project"}, None, id="relative"),
            pytest.param({"branch": ""}, None, id="empty-branch"),
            pytest.param({}, "{", id="malformed"),
        ],
    )
    def test_rejects_invalid_recovered_checkout(
        self, valid_run: tuple[Path, dict[str, object]], change: dict[str, object], raw: str | None
    ) -> None:
        """An unusable recovery receipt must not silently show the original checkout.

        A valid original receipt exists beside a recovered one that is legacy, relative, branchless or unparsable;
        derivation must fail instead of falling back to the original checkout.
        """
        run, result = valid_run
        receipt = {
            "schema_version": 2,
            "status": "prepared",
            "branch": "fix/selection",
            "worktree": "/workspace/project",
        }
        (run / "remediation-branch.json").write_text(json.dumps(receipt), encoding="utf-8")
        (run / "remediation-branch-recovered.json").write_text(
            raw or json.dumps({**receipt, **change}), encoding="utf-8"
        )

        with pytest.raises(HELPER.DeriveError, match=r"unreadable-json|checkout-receipt-invalid"):
            HELPER.derive_handoff(
                run, result["metadata"], _draft_handoff(run), result["confidence"], result["artifact_path"]
            )

    @pytest.mark.parametrize(
        ("selection_version", "handoff_version"),
        [pytest.param(4, 15, id="historical-selection-new-handoff"), pytest.param(5, 15, id="current")],
    )
    def test_marks_selection_independently_of_outcome(
        self, valid_run: tuple[Path, dict[str, object]], selection_version: int, handoff_version: int
    ) -> None:
        """Keep all findings visible and count selection indexes only over selectable items.

        A run holds one closed, one omitted and one chosen finding under a historical or current selection scope; the
        handoff must mark them Closed/No/Yes, render the Selected column, pass the candidate validator, and be refused
        once a row carries a boolean mark.
        """
        run, result = valid_run
        metadata = copy.deepcopy(result["metadata"])
        metadata["resolution_scope"].update(presentation_version=selection_version, selected_indexes=[2])
        original = metadata["final_resolution_table"]["items"][0]
        items = [_marked_item(original, identity, selectable) for identity, selectable in _SELECTION_MARKS]
        metadata["final_resolution_table"]["items"] = items

        handoff = HELPER.derive_handoff(
            run, metadata, _draft_handoff(run), result["confidence"], result["artifact_path"]
        )

        assert handoff["presentation_version"] == handoff_version
        assert handoff["action_policy"] == "owned"
        assert [(row["id"], row["selected"]) for row in handoff["tables"][0]["rows"]] == [
            ("CLOSED", "Closed"),
            ("OMITTED", "No"),
            ("CHOSEN", "Yes"),
        ]
        rendered = HELPER.final_handoff.render_handoff(handoff)
        assert "| ID | Severity | Finding | Selected | Outcome | How resolved |" in rendered
        assert all(
            f"| {identity} | {selected} | Implemented | Added the guard. |" in rendered
            for identity, selected in [("CLOSED", "Closed"), ("OMITTED", "No"), ("CHOSEN", "Yes")]
        )
        assert "| Resolution |" not in rendered
        assert "[Action items](" in rendered
        VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff, candidate=True)
        handoff["tables"][0]["rows"][0]["selected"] = True
        with pytest.raises(SystemExit, match="row-coverage-mismatch"):
            VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff, candidate=True)

    def test_retains_historical_boolean_selection_rendering(self, valid_run: tuple[Path, dict[str, object]]) -> None:
        """Keep version-five reports readable without applying current three-state semantics.

        A handoff downgraded to presentation five carries boolean selection marks; it must still render and validate as
        a historical report, while the same handoff as a fresh candidate must demand the current presentation.
        """
        run, result = valid_run
        metadata = copy.deepcopy(result["metadata"])
        metadata["resolution_scope"]["presentation_version"] = 4
        handoff = HELPER.derive_handoff(
            run, metadata, _draft_handoff(run), result["confidence"], result["artifact_path"]
        )
        handoff["presentation_version"] = 5
        del handoff["action_policy"]
        handoff["tables"][0]["rows"] = [
            {**row, "selected": row["selected"] == "Yes"} for row in handoff["tables"][0]["rows"]
        ]
        rendered = HELPER.final_handoff.render_handoff(handoff)
        assert "| Selected |" in rendered
        assert "| Closed |" not in rendered
        VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff)
        with pytest.raises(SystemExit, match="remediation-current-handoff-presentation-v6-required"):
            VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff, candidate=True)

    @pytest.mark.parametrize(
        "selected",
        [
            pytest.param({}, id="missing"),
            pytest.param({"selected": 1}, id="integer"),
            pytest.param({"selected": True}, id="boolean"),
            pytest.param({"selected": "Unknown"}, id="unknown-label"),
        ],
    )
    def test_rejects_missing_or_invalid_selection_status(
        self, valid_run: tuple[Path, dict[str, object]], selected: dict[str, object]
    ) -> None:
        """A current selection mark must use the explicit three-state contract.

        The first handoff row loses its ``selected`` mark or carries a value outside Closed/No/Yes; rendering must
        refuse.
        """
        run, result = valid_run
        metadata = copy.deepcopy(result["metadata"])
        metadata["resolution_scope"]["presentation_version"] = 4
        handoff = HELPER.derive_handoff(
            run, metadata, _draft_handoff(run), result["confidence"], result["artifact_path"]
        )
        rows = handoff["tables"][0]["rows"]
        rows[0] = {**_without_selected(rows[0]), **selected}
        with pytest.raises(ValueError, match="remediation-row-selected-invalid"):
            HELPER.final_handoff.render_handoff(handoff)

    def test_candidate_cannot_hide_selection_with_historical_handoff(
        self,
        valid_run: tuple[Path, dict[str, object]],
    ) -> None:
        """Historical reports remain readable, but a fresh scope-four candidate must show selection.

        A scope-four handoff is rewritten to presentation four with no selection marks; it renders without a Selected
        column and validates as history, yet the candidate validator must demand the current presentation.
        """
        run, result = valid_run
        metadata = copy.deepcopy(result["metadata"])
        metadata["resolution_scope"]["presentation_version"] = 4
        handoff = HELPER.derive_handoff(
            run, metadata, _draft_handoff(run), result["confidence"], result["artifact_path"]
        )
        handoff["presentation_version"] = 4
        del handoff["action_policy"]
        handoff["tables"][0]["rows"] = [_without_selected(row) for row in handoff["tables"][0]["rows"]]
        assert "| Selected |" not in HELPER.final_handoff.render_handoff(handoff)
        VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff)
        with pytest.raises(SystemExit, match="remediation-current-handoff-presentation-v6-required"):
            VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff, candidate=True)

    def test_validator_preserves_historical_handoff_but_rejects_new_candidate(self, tmp_path: Path) -> None:
        """Distinguish a promoted scope-four report from a fresh candidate at the normal validator entrypoint.

        A complete run is rebuilt as a historical presentation-four report; the public validator must accept it as the
        canonical result, refuse it as a candidate, and accept both once the handoff is re-derived to the current form.
        The two validation phases are one lifecycle, so they stay in a single test.
        """
        candidate = _write_remediation_candidate(tmp_path, "code")
        result = json.loads(candidate.read_text(encoding="utf-8"))
        metadata = result["metadata"]
        metadata["resolution_scope"]["presentation_version"] = 4
        inventory_path = tmp_path / "selection.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory["presentation_version"] = 4
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
        (tmp_path / "resolution-scope.md").write_bytes(HELPER.final_handoff.render_selection(inventory).encode("utf-8"))
        _set_resolved_how(metadata, "Verified without code changes: Existing regression passes.")
        HELPER.render_ledger(tmp_path, metadata)
        handoff = HELPER.derive_handoff(
            tmp_path, metadata, _draft_handoff(tmp_path), result["confidence"], result["artifact_path"]
        )
        handoff["presentation_version"] = 4
        del handoff["action_policy"]
        handoff["tables"][0]["rows"] = [_without_selected(row) for row in handoff["tables"][0]["rows"]]
        (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
        HELPER._bind_handoff(tmp_path, metadata, handoff)
        canonical = tmp_path / "result.json"
        canonical.write_text(json.dumps(result), encoding="utf-8")
        candidate.write_bytes(canonical.read_bytes())

        VALIDATOR.validate("code-remediate", tmp_path, canonical)
        with pytest.raises(SystemExit, match="remediation-current-handoff-presentation-v6-required"):
            VALIDATOR.validate("code-remediate", tmp_path, candidate)

        current = HELPER.derive_handoff(tmp_path, metadata, handoff, result["confidence"], result["artifact_path"])
        (tmp_path / "final-handoff.json").write_text(json.dumps(current), encoding="utf-8")
        HELPER._bind_handoff(tmp_path, metadata, current)
        candidate.write_text(json.dumps(result), encoding="utf-8")
        canonical.write_bytes(candidate.read_bytes())
        VALIDATOR.validate("code-remediate", tmp_path, candidate)
        VALIDATOR.validate("code-remediate", tmp_path, canonical)


def _close_items_for_current_selection(run: Path, result: dict[str, object]) -> None:
    """Rewrite a valid run's items, ledger, selection inventory and scope as a current (version 5) selection."""
    for item in result["metadata"]["final_resolution_table"]["items"]:
        item["resolved_how"] = (
            "Implemented: Added the guard."
            if item["resolution_status"] == "implemented"
            else "Verified without code changes: Existing regression passes."
        )
    HELPER.render_ledger(run, result["metadata"])
    inventory_path = run / "selection.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["presentation_version"] = 5
    for item in inventory["items"]:
        if item["selectable"]:
            item["resolution_proposal"] = "Correct [parser](<src/package/parser.py>) and verify the regression."
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    (run / "resolution-scope.md").write_text(
        HELPER.final_handoff.render_selection(inventory), encoding="utf-8", newline="\n"
    )


def _finalize_judgement_drafts(
    run: Path, result: dict[str, object], drafts: Path, owner: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, object]]:
    """Finalize with ``--promote`` from drafts carrying only judgement fields; return exit code and summary."""
    (drafts / "metadata.json").write_text(json.dumps(_stale_metadata(result)), encoding="utf-8")
    draft = _draft_handoff(run)
    row_id = result["metadata"]["final_resolution_table"]["items"][0]["input_item_id"]
    draft["remaining"] = [
        {
            "row_id": row_id,
            "item": "A verification step remains.",
            "owner": owner,
            "next_action": "Run the required check.",
        }
    ]
    draft["next_steps"] = [row_id]
    (drafts / "handoff.json").write_text(json.dumps(draft), encoding="utf-8")
    argv = ["finalize", "--run", str(run), "--metadata", str(drafts / "metadata.json")]
    argv += ["--handoff", str(drafts / "handoff.json"), "--status", result["status"]]
    argv += ["--confidence", str(result["confidence"]), "--artifact-path", result["artifact_path"], "--promote"]
    exit_code = HELPER.main(argv)
    return exit_code, json.loads(capsys.readouterr().out)


def _assert_promoted_for_owner(run: Path, exit_code: int, summary: dict[str, object], owner: str) -> None:
    """Assert the finalize summary passed in the four documented steps and the promoted result names the owner."""
    assert (exit_code, summary["status"], summary["errors"], summary["promoted"]) == (0, "pass", [], True), summary[
        "errors"
    ]
    assert [step["step"] for step in summary["steps"]] == ["derive", "render", "write-result", "validate"]
    VALIDATOR.validate("code-remediate", run, run / "result.json")
    final_text = (run / "final.md").read_text(encoding="utf-8")
    assert final_text.index("**Artifact**") < final_text.index("**Recommendations / next steps**")
    assert final_text.endswith(f"| {owner} | A verification step remains. | Run the required check. |\n")


def _write_worktree_delivery(run: Path) -> dict[str, object]:
    """Write the local destination evidence for an explicitly selected linked PR worktree."""
    checkout = (run.parent / "worktree").as_posix()
    common = (run.parent / "caller" / ".git").as_posix()
    delivery = {
        "schema_version": 1,
        "status": "worktree-only",
        "checkout": checkout,
        "branch": "fix/guard",
        "head": "b" * 40,
        "common_dir": common,
    }
    locator = {
        "schema_version": 1,
        "worktree": checkout,
        "invoking_checkout": (run.parent / "caller").as_posix(),
        "git_common_dir": common,
        "initial_head": "c" * 40,
    }
    (run / "remediation-worktree.json").write_text(json.dumps(locator), encoding="utf-8")
    (run / "worktree-delivery.json").write_text(json.dumps(delivery), encoding="utf-8")
    (run / "worktree-delivery.md").write_text("Recorded local verification.\n", encoding="utf-8")
    return delivery


def _write_publication_offer(run: Path, delivery: dict[str, object], *, ref: str = "refs/heads/fix/guard") -> str:
    """Record the offer scoped to the delivered commit and exact remote tip."""
    offer = {
        "checkout": delivery["checkout"],
        "branch": delivery["branch"],
        "common_dir": delivery["common_dir"],
        "url": "https://github.com/acme/widgets.git",
        "ref": ref,
        "source_oid": delivery["head"],
        "expected_oid": "a" * 40,
    }
    body = json.dumps(offer).encode()
    (run / "publication-offer.json").write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def _write_publication_attempt(run: Path, delivery: dict[str, object], *, ref: str = "refs/heads/fix/guard") -> str:
    """Bind a consumed native approval to the retained offer."""
    digest = _write_publication_offer(run, delivery, ref=ref)
    attempt = {"offer_digest": digest, "decision_id": f"pr-worktree-publication-{digest}", "answer": "Approve"}
    (run / "publication-attempt.json").write_text(json.dumps(attempt), encoding="utf-8")
    return digest


def test_resumes_local_delivery_without_a_publication_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Let a delivered run finish locally after interruption while the optional question was open."""
    delivery = _write_worktree_delivery(tmp_path)
    _write_publication_offer(tmp_path, delivery)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    exit_code = HELPER.main(["publication-resume", "--run", str(tmp_path)])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["publication"]["status"] == "skipped"
    assert result["evidence"] == "publication-offer.json"
    assert result["needs_terminal_record"] is True
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize(
    ("marker", "status", "needs_terminal_record"),
    [
        pytest.param(False, "skipped", True, id="approved-but-nothing-sent"),
        pytest.param(True, "unverified", False, id="started-attempt"),
    ],
)
def test_consumed_publication_resumes_without_remote_read_or_reoffer(
    tmp_path: Path, capsys: pytest.CaptureFixture, marker: bool, status: str, needs_terminal_record: bool
) -> None:
    """Read consumed local evidence without persisting a new offer or guessing that the push ran.

    The execution marker is written before anything is sent, so without it the approved attempt sent nothing and resumes
    as ``skipped``; with it, an outcome nobody recorded stays ``unverified``.
    """
    delivery = _write_worktree_delivery(tmp_path)
    digest = _write_publication_attempt(tmp_path, delivery)
    if marker:
        (tmp_path / "publication-executed").write_bytes(f"{digest}\n".encode())
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    exit_code = HELPER.main(["publication-resume", "--run", str(tmp_path)])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["delivery"] == delivery
    assert result["publication"]["status"] == status
    assert result["evidence"] == "publication-attempt.json"
    assert result["needs_terminal_record"] is needs_terminal_record
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize(
    ("marker", "status", "accepted"),
    [
        pytest.param(None, "skipped", True, id="unsent-skipped"),
        pytest.param(None, "failed", False, id="unsent-cannot-claim-failed"),
        pytest.param(None, "unverified", False, id="unsent-cannot-claim-unverified"),
        pytest.param(None, "declined", False, id="approved-cannot-claim-declined"),
        pytest.param("file", "failed", True, id="started-failed"),
        pytest.param("file", "unverified", True, id="started-unverified"),
        pytest.param("file", "skipped", False, id="started-cannot-claim-skipped"),
        pytest.param("empty", "skipped", False, id="damaged-marker-cannot-claim-skipped"),
    ],
)
def test_the_execution_marker_decides_what_an_approved_attempt_can_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], marker: str | None, status: str, accepted: bool
) -> None:
    """Without a marker nothing was sent, so only ``skipped`` is true; any marker entry means the attempt started."""
    delivery = _write_worktree_delivery(tmp_path)
    digest = _write_publication_attempt(tmp_path, delivery)
    if marker == "file":
        (tmp_path / "publication-executed").write_bytes(f"{digest}\n".encode())
    elif marker == "empty":
        (tmp_path / "publication-executed").touch()
    terminal = {"schema_version": 1, "status": status, "reason": "Recorded outcome.", "observed_oid": None}
    (tmp_path / "publication.json").write_text(json.dumps(terminal), encoding="utf-8")

    exit_code = HELPER.main(["publication-resume", "--run", str(tmp_path)])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == int(not accepted)
    if accepted:
        assert result["publication"]["status"] == status
        assert result["needs_terminal_record"] is False
    else:
        assert result["code"] == "publication-disposition-invalid"


def test_a_push_with_an_unknown_outcome_resolves_unverified_never_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The publication helper's ``unverified`` report is a terminal status the finalizer accepts once the marker exists.

    The remote may hold the update, but without the transport's ``pushed`` receipt a ``verified`` claim is refused.
    """
    publication = load_module("codex_rig_worktree_publication", PLUGIN_ROOT / "shared" / "worktree_publication.py")
    delivery = _write_worktree_delivery(tmp_path)
    digest = _write_publication_attempt(tmp_path, delivery)

    def start_then_lose_the_verdict(*args: object, **kwargs: object) -> None:
        """Spend the attempt, then end like a push that timed out after sending."""
        kwargs["before_push"]()
        raise ValueError("publication-push-outcome-unknown")

    monkeypatch.setattr(publication, "_isolated_push", start_then_lose_the_verdict)
    argv = ["--run", str(tmp_path), "--offer-digest", digest, "--credential-helper", "osxkeychain"]
    assert publication.main(argv) == 1
    reported = json.loads(capsys.readouterr().out)
    assert reported["status"] == "unverified"

    terminal = {"schema_version": 1, "status": reported["status"], "reason": reported["reason"], "observed_oid": None}
    (tmp_path / "publication.json").write_text(json.dumps(terminal), encoding="utf-8")
    assert HELPER.main(["publication-resume", "--run", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["publication"]["status"] == "unverified"

    claimed = {**terminal, "status": "verified", "observed_oid": delivery["head"]}
    (tmp_path / "publication.json").write_text(json.dumps(claimed), encoding="utf-8")
    assert HELPER.main(["publication-resume", "--run", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "publication-verification-unproven"


@pytest.mark.parametrize(
    "status",
    [pytest.param(["skipped"], id="list"), pytest.param({"skipped": 1}, id="object")],
)
def test_a_terminal_status_that_is_not_a_string_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], status: object
) -> None:
    """A malformed status is an invalid record, never a crash of the report-only resume."""
    delivery = _write_worktree_delivery(tmp_path)
    _write_publication_attempt(tmp_path, delivery)
    terminal = {"schema_version": 1, "status": status, "reason": "Recorded outcome.", "observed_oid": None}
    (tmp_path / "publication.json").write_text(json.dumps(terminal), encoding="utf-8")

    assert HELPER.main(["publication-resume", "--run", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "publication-disposition-invalid"


def test_an_execution_result_without_its_marker_is_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A transport receipt with no marker contradicts the helper's order, so it never resumes as nothing sent."""
    delivery = _write_worktree_delivery(tmp_path)
    digest = _write_publication_attempt(tmp_path, delivery)
    (tmp_path / "publication-execution-result.json").write_text(
        json.dumps({"status": "pushed", "offer_digest": digest}), encoding="utf-8"
    )

    assert HELPER.main(["publication-resume", "--run", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "publication-attempt-invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("branch", "fix/other", id="wrong-local-branch"),
        pytest.param("common_dir", "C:/work/caller/.git", id="wrong-git-common-dir"),
    ],
)
def test_publication_offer_requires_the_delivered_local_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], field: str, value: str
) -> None:
    """Reject an offer that belongs to a different local branch or Git common directory."""
    delivery = _write_worktree_delivery(tmp_path)
    _write_publication_attempt(tmp_path, {**delivery, field: value})

    assert HELPER.main(["publication-resume", "--run", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "publication-offer-invalid"


@pytest.mark.parametrize(
    ("transport_status", "remote_oid", "remote_url", "expected_status"),
    [
        pytest.param(
            None, "b" * 40, "acme/widgets/git/refs/heads/fix/stra%C3%9Fe%25route", "fail", id="interrupted-push"
        ),
        pytest.param(
            "pushed", "b" * 40, "acme/widgets/git/refs/heads/fix/stra%C3%9Fe%25route", "pass", id="encoded-ref-agrees"
        ),
        pytest.param(
            "pushed", "a" * 40, "acme/widgets/git/refs/heads/fix/stra%C3%9Fe%25route", "fail", id="readback-disagrees"
        ),
        pytest.param(
            "pushed", "b" * 40, "fork/widgets/git/refs/heads/fix/stra%C3%9Fe%25route", "fail", id="crossed-repository"
        ),
        pytest.param("pushed", "b" * 40, "acme/widgets/git/refs/heads/fix/other", "fail", id="crossed-branch-url"),
    ],
)
def test_verified_publication_requires_successful_execution_and_matching_ref(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    transport_status: str | None,
    remote_oid: str,
    remote_url: str,
    expected_status: str,
) -> None:
    """Require both transport success and the exact remote ref readback before accepting verified publication."""
    delivery = _write_worktree_delivery(tmp_path)
    ref = "refs/heads/fix/straße%route"
    digest = _write_publication_attempt(tmp_path, delivery, ref=ref)
    (tmp_path / "publication-executed").write_bytes(f"{digest}\n".encode())
    (tmp_path / "publication-execution-result.json").write_text(
        json.dumps({"status": transport_status, "offer_digest": digest}), encoding="utf-8"
    )
    (tmp_path / "publication-readback.json").write_text(
        json.dumps(
            {
                "ref": ref,
                "url": f"https://api.github.com/repos/{remote_url}",
                "object": {"type": "commit", "sha": remote_oid},
            }
        ),
        encoding="utf-8",
    )
    terminal = {"schema_version": 1, "status": "verified", "reason": "Ref matches", "observed_oid": delivery["head"]}
    (tmp_path / "publication.json").write_text(json.dumps(terminal), encoding="utf-8")

    exit_code = HELPER.main(["publication-resume", "--run", str(tmp_path)])

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == expected_status
    assert exit_code == int(expected_status == "fail")
    readback_digest = hashlib.sha256((tmp_path / "publication-readback.json").read_bytes()).hexdigest()
    assert result.get("publication", {}).get("readback_sha256") == (
        readback_digest if expected_status == "pass" else None
    )


class TestAllErrors:
    """Protect the one-pass validator report and its command line."""

    def test_reports_independent_failures_with_hints(self, valid_run: tuple[Path, dict[str, object]]) -> None:
        """Report every independent failure in one pass while the fail-fast entry point still stops at the first.

        Two unrelated defects, a stale plan copy and a missing unresolved-work note, must both appear with repair hints
        so one repair round fixes them; neither may be reported as passed.
        """
        run, result = valid_run
        result_path = run / "result.candidate.json"
        result["metadata"]["resolution_workplan"]["work_buckets"][0]["owner"] = "parent "
        result_path.write_text(json.dumps(result), encoding="utf-8")
        (run / "unresolved.txt").unlink()

        report = VALIDATOR.collect_errors("code-remediate", run, result_path)

        codes = {error["code"] for error in report["errors"]}
        assert report["status"] == "fail"
        assert "code-remediate-work-bucket-plan-content-mismatch" in codes
        assert any(code.startswith("missing-artifact:") and code.endswith("unresolved.txt") for code in codes)
        assert all(error["hint"] for error in report["errors"])
        with pytest.raises(SystemExit):
            VALIDATOR.validate("code-remediate", run, result_path)

    def test_missing_resolution_summary_has_actionable_hint(self, valid_run: tuple[Path, dict[str, object]]) -> None:
        """Explain how to repair a missing summary without inventing its judgments.

        The action-items summary heading is renamed away; the one-pass report must carry the missing-section error with
        a hint that tells the author what to request, count, close and retry.
        """
        run, _ = valid_run
        action_items = run / "action-items.md"
        content = action_items.read_text(encoding="utf-8")
        action_items.write_text(content.replace("## Final Resolution Summary", "## Other Summary"), encoding="utf-8")

        report = VALIDATOR.collect_errors("code-remediate", run, run / "result.candidate.json")

        error = next(
            error
            for error in report["errors"]
            if error["code"] == "missing-artifact-section:action-items.md:Final Resolution Summary"
        )
        assert all(term in error["hint"] for term in ("request", "counts", "closed", "retry"))

    def test_passes_valid_run_and_matches_fail_fast(self, valid_run: tuple[Path, dict[str, object]]) -> None:
        """Accept exactly the runs the fail-fast validator accepts.

        A fully valid run must yield an empty pass report from the one-pass collector, and the fail-fast entry point
        must agree by not raising; a one-pass report that disagreed would send authors into pointless repair rounds.
        """
        run, _ = valid_run

        report = VALIDATOR.collect_errors("code-remediate", run, run / "result.candidate.json")

        assert report == {"status": "pass", "errors": [], "not_run": []}
        VALIDATOR.validate("code-remediate", run, run / "result.candidate.json")

    def test_marks_dependent_steps_not_run(self, tmp_path: Path) -> None:
        """Never let a step whose prerequisite failed read as a pass.

        The candidate holds only a status, so the result step fails; every later step depends on it and must be listed
        as not run rather than passed.
        """
        (tmp_path / "result.candidate.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")

        report = VALIDATOR.collect_errors("code-remediate", tmp_path, tmp_path / "result.candidate.json")

        assert report["status"] == "fail"
        assert report["errors"][0]["step"] == "result"
        assert {entry["step"] for entry in report["not_run"]} >= {"gates", "final-handoff", "code-remediate-workplan"}

    def test_cli_prints_json_report_with_failing_exit(
        self, valid_run: tuple[Path, dict[str, object]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Expose the one-pass report through the validator command line with a failing exit status.

        A candidate with a non-boolean relevance flag is validated with ``--all-errors``; the command must print the
        JSON report with failing status and listed errors, and exit 1 so scripts can branch on it.
        """
        run, result = valid_run
        result["metadata"]["pr_relevance"]["evaluated"] = "yes"
        (run / "result.candidate.json").write_text(json.dumps(result), encoding="utf-8")
        argv = ["validate-artifacts.py", "--skill", "code-remediate", "--out", str(run)]
        argv += ["--result", str(run / "result.candidate.json"), "--all-errors"]

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("sys.argv", argv)
            exit_code = VALIDATOR.main()
        report = json.loads(capsys.readouterr().out)

        assert (exit_code, report["status"]) == (1, "fail")
        assert report["errors"]


class TestWorkplan:
    """Protect the rendered workplan sections and the empty-selection form."""

    def test_render_keeps_agent_sections_and_passes_document_checks(
        self,
        valid_run: tuple[Path, dict[str, object]],
    ) -> None:
        """Rewrite only the managed workplan sections, keep upfront decisions, and satisfy the workplan validator.

        The agent appended an ``Upfront Decisions`` section to the rendered workplan; a refresh must leave it intact,
        keep a single ``Parallel Approval`` section, and produce a document the workplan validator accepts.
        """
        run, result = valid_run
        workplan = run / "resolution-workplan.md"
        workplan.write_text(
            workplan.read_text(encoding="utf-8") + "\n## Upfront Decisions\n\nCommit preference: all at once.\n",
            encoding="utf-8",
        )
        metadata = copy.deepcopy(result["metadata"])

        HELPER.render_workplan(run, metadata, "One selected item forms one coherent bucket.")

        text = workplan.read_text(encoding="utf-8")
        assert "## Upfront Decisions\n\nCommit preference: all at once." in text
        assert text.count("## Parallel Approval") == 1
        VALIDATOR._validate_code_remediate_workplan(metadata, run)

    def test_empty_selection_needs_no_fabricated_workplan(self, valid_run: tuple[Path, dict[str, object]]) -> None:
        """Let a no-findings run finish without inventing a plan while rejecting hidden work.

        A live remediation found no selectable findings, but path validation previously blocked its handoff before
        checking the empty selection. Nonempty selections must still provide the approved plan and its evidence.
        """
        run, result = valid_run
        metadata = copy.deepcopy(result["metadata"])
        metadata["resolution_scope"]["selected_indexes"] = []
        workplan = metadata["resolution_workplan"]
        workplan["work_buckets"] = []
        _clear_workplan(workplan)

        VALIDATOR._validate_code_remediate_workplan(metadata, run)
        workplan["parent_owned_groups"] = 1
        with pytest.raises(SystemExit, match="code-remediate-empty-selection-has-work-buckets"):
            VALIDATOR._validate_code_remediate_workplan(metadata, run)
        workplan["parent_owned_groups"] = 0
        metadata["resolution_scope"]["selected_indexes"] = [1]
        with pytest.raises(SystemExit, match="code-remediate-invalid-resolution-workplan:workplan_path"):
            VALIDATOR._validate_code_remediate_workplan(metadata, run)

    def test_render_requires_reason_for_default_parent_only_plan(
        self,
        valid_run: tuple[Path, dict[str, object]],
    ) -> None:
        """Refuse to render a default parent-only plan without a concrete ineligibility reason.

        A refresh may reuse the reason already rendered in the workplan, so this case removes it first: with no argument
        and no recorded reason, nothing can justify the parent-only route.
        """
        run, result = valid_run
        workplan = run / "resolution-workplan.md"
        text = workplan.read_text(encoding="utf-8")
        workplan.write_text(
            "\n".join(line for line in text.splitlines() if not line.startswith("Ineligibility reason:"))
        )

        with pytest.raises(HELPER.DeriveError, match="ineligibility-reason-required"):
            HELPER.render_workplan(run, copy.deepcopy(result["metadata"]), None)


class TestDerivedMetadata:
    """Protect metadata derived from machine files instead of hand copies."""

    def test_restores_every_hand_copied_field(self, valid_run: tuple[Path, dict[str, object]]) -> None:
        """Rebuild scope, workplan, and item identity from machine files with canonical required columns.

        A drifted hand copy of the plan, digest, counts, deferred indexes, or item names is the failure family this
        helper removes; the derived values must equal the ones a validator-accepted run carries.
        """
        run, result = valid_run
        expected = copy.deepcopy(result["metadata"])
        expected["final_resolution_table"]["required_columns"] = sorted(
            VALIDATOR.CODE_REMEDIATE_FINAL_TABLE_REQUIRED_COLUMNS
        )

        derived = HELPER.derive_metadata(run, _stale_metadata(result))

        keys = ("resolution_scope", "resolution_workplan", "final_resolution_table", "pr_relevance")
        assert {key: derived[key] for key in keys} == {key: expected[key] for key in keys}

    def test_rejects_plan_digest_that_differs_from_approval(self, valid_run: tuple[Path, dict[str, object]]) -> None:
        """Stop when the dispatch record was approved for different plan bytes instead of rebinding it.

        The bucket plan file gains a byte after approval; derivation must raise the digest-mismatch error, because
        silently rebinding would let unapproved work run under an old approval.
        """
        run, result = valid_run
        plan = run / "work-bucket-plan.json"
        plan.write_text(plan.read_text(encoding="utf-8") + " ", encoding="utf-8")

        with pytest.raises(HELPER.DeriveError, match="parallel-approval-plan-digest-mismatch"):
            HELPER.derive_metadata(run, copy.deepcopy(result["metadata"]))


def test_skill_routes_bookkeeping_through_the_derivation_helper() -> None:
    """Keep the remediation skill on derived workplan sections, one-call finalize, and one-pass validation.

    The skill text (whitespace-normalized across its reference files) and the helper CLI contract must keep naming the
    helper commands and the no-hand-copy rules; this pins skill-to-helper routing, so drift here means wording moved.
    """
    skill = " ".join(
        read_with_references(
            PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md", "implementation.md", "completion.md"
        ).split()
    )
    contract = (PLUGIN_ROOT / "shared" / "helper-cli-contract.md").read_text(encoding="utf-8")

    assert "remediation_finalize.py workplan --help" in skill
    assert "Do not hand-write those four sections" in skill
    assert "remediation_finalize.py finalize --help" in skill
    assert "repair every listed error in one round from its hint, then rerun `finalize`" in skill
    assert "Never hand-copy a derived field." in skill
    assert "one `remediation_finalize.py finalize --promote` call" in skill
    assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/remediation_finalize.py --help" in contract
    assert "`--all-errors`" in contract


class TestReviewFinalize:
    """Protect one-command finalization of code-review runs."""

    @pytest.mark.parametrize("newline", [pytest.param(b"\n", id="lf"), pytest.param(b"\r\n", id="crlf")])
    def test_derives_shared_fields_and_runs_both_validators(
        self, assessed_pr: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], newline: bytes
    ) -> None:
        """Finalize an assessed PR review in one call: shared handoff fields, render, write, both validators, promote.

        Snapshot judgments remain agent-authored; canonical action content and shared handoff fields are derived. The
        promoted result must still complete through the review finder, which reruns both validators.
        """
        run = assessed_pr
        report = run / "review-notes.md"
        full_report = report.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline)
        report.write_bytes(full_report)
        result_path = run / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result_path.unlink()
        drafts = tmp_path / "drafts"
        drafts.mkdir()
        metadata = {key: value for key, value in result["metadata"].items() if key != "final_handoff"}
        handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
        handoff = {k: v for k, v in handoff.items() if k not in ("verification", "confidence", "artifacts")}
        (drafts / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (drafts / "handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
        argv = ["finalize", "--skill", "code-review", "--parent-thread-id", "thread", "--run", str(run)]
        argv += ["--metadata", str(drafts / "metadata.json"), "--handoff", str(drafts / "handoff.json")]
        argv += ["--status", "pass", "--confidence", "0.95", "--artifact-path", str(result_path), "--promote"]

        exit_code = HELPER.main(argv)
        summary = json.loads(capsys.readouterr().out)
        finished = subprocess.run(
            [sys.executable, str(FINDER), "--complete-run", str(run), "--parent-thread-id", "thread"],
            capture_output=True,
        )

        assert (exit_code, summary["status"], summary["promoted"]) == (0, "pass", True), summary
        assert [step["step"] for step in summary["steps"]] == [
            "derive",
            "render",
            "write-result",
            "review-validate",
            "validate",
        ]
        assert finished.returncode == 0, finished.stderr.decode()
        assert json.loads((run / "final-handoff.json").read_text())["presentation_version"] == 15
        assert (run / "review-notes.md").read_bytes().startswith(full_report)
        reviewed_head = json.loads((run / "pr-routing.json").read_text())["head_oid"]
        assert f"Reviewed commit: {reviewed_head}" in (run / "review-notes.md").read_text()
        assert b"- Report: " in finished.stdout
        assert b"| Report |" not in finished.stdout
        finalized_report = report.read_bytes()
        assert HELPER.main(argv) == 0
        assert report.read_bytes() == finalized_report

    def test_stops_on_review_validator_failure(
        self, assessed_pr: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Never promote a review candidate the review-specific validator rejects.

        The assessed PR run is finalized with a parent thread id that differs from its provenance; finalize must exit 1
        at the review-validate step and leave no promoted result file behind.
        """
        run = assessed_pr
        result_path = run / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result_path.unlink()
        (tmp_path / "metadata.json").write_text(json.dumps(result["metadata"]), encoding="utf-8")
        argv = ["finalize", "--skill", "code-review", "--parent-thread-id", "other-thread", "--run", str(run)]
        argv += ["--metadata", str(tmp_path / "metadata.json"), "--handoff", str(run / "final-handoff.json")]
        argv += ["--status", "pass", "--confidence", "0.95", "--artifact-path", str(result_path), "--promote"]

        exit_code = HELPER.main(argv)
        summary = json.loads(capsys.readouterr().out)

        assert (exit_code, summary["status"], summary["promoted"]) == (1, "fail", False)
        assert summary["steps"][-1]["step"] == "review-validate"
        assert not result_path.exists()

    @pytest.mark.integration
    def test_repairs_copied_blocker_content_before_completion(
        self, assessed_pr: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Canonical blocker facts survive stale presentation through completion and report admission.

        The fallback drafts and the notes table carry a stale proposal, title and evidence; finalize must restore the
        canonical text, the review finder must then complete the run and accept the result, and a rerun must be byte-
        stable. Finalize, completion and rerun are one lifecycle over real subprocesses.
        """
        run = assessed_pr
        prepared = _finalize_parent_fallback(run)
        assert prepared.returncode == 0, prepared.stdout + prepared.stderr
        result_path = run / "result.json"
        result_path.unlink()
        notes = run / "review-notes.md"
        original = notes.read_text(encoding="utf-8")
        notes.write_text(
            original.replace("Run the configured mypy check in the project environment.", "Stale proposal"),
            encoding="utf-8",
        )
        draft = run / "fallback-drafts/handoff.json"
        handoff = json.loads(draft.read_bytes())
        row = handoff["tables"][-1]["rows"][0]
        row["title"] = "Stale title"
        row["cells"][1:3] = ["Stale proposal", "Stale evidence"]
        draft.write_text(json.dumps(handoff), encoding="utf-8")
        argv = ["finalize", "--skill", "code-review", "--parent-thread-id", "thread", "--run", str(run)]
        argv += ["--metadata", str(run / "fallback-drafts/metadata.json"), "--handoff", str(draft)]
        argv += ["--status", "fail", "--confidence", "0.95", "--artifact-path", str(result_path), "--promote"]

        assert HELPER.main(argv) == 0, capsys.readouterr().out
        summary = json.loads(capsys.readouterr().out)
        assert summary["promoted"] is True
        assert notes.read_text(encoding="utf-8") == original
        assert json.loads(result_path.read_bytes())["status"] == "fail"
        completed = [
            subprocess.run(
                [sys.executable, str(FINDER), *action, "--parent-thread-id", "thread"], capture_output=True, text=True
            )
            for action in (["--complete-run", str(run)], ["--result", str(result_path)])
        ]
        assert [done.returncode for done in completed] == [0, 0], [done.stderr for done in completed]
        before = notes.read_bytes()
        assert HELPER.main(argv) == 0, capsys.readouterr().out
        assert notes.read_bytes() == before

    def test_failure_retains_structured_errors_and_next_action(
        self, assessed_pr: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A provenance rejection retains each check and a recovery action without promotion.

        Finalizing with a mismatching parent thread id fails; the summary must list every error with step, code and
        hint, name the code-review owner and a provenance recovery action, and leave the run unpromoted.
        """
        run = assessed_pr
        result = json.loads((run / "result.json").read_bytes())
        (run / "result.json").unlink()
        metadata = tmp_path / "metadata.json"
        metadata.write_text(json.dumps(result["metadata"]), encoding="utf-8")
        argv = ["finalize", "--skill", "code-review", "--parent-thread-id", "other-thread", "--run", str(run)]
        argv += ["--metadata", str(metadata), "--handoff", str(run / "final-handoff.json")]
        argv += ["--status", "pass", "--confidence", "0.95", "--artifact-path", str(run / "result.json"), "--promote"]

        assert HELPER.main(argv) == 1
        summary = json.loads(capsys.readouterr().out)
        assert summary["errors"]
        assert all(error["step"] and error["code"] and error["hint"] for error in summary["errors"])
        assert summary["next_action"]["owner"] == "code-review"
        assert "provenance" in summary["next_action"]["action"]
        assert "validators" in summary["next_action"]["resume_condition"]
        assert summary["promoted"] is False
        assert not (run / "result.json").exists()


def _review_records_and_rows(metadata: dict, record_update: dict) -> tuple[list[dict], list[dict]]:
    """Give each finding and blocker pipe- and newline-bearing content; return records with stale handoff rows."""
    records = metadata["review_findings"] + metadata["operational_blockers"]
    rows = []
    for record in records:
        record["required_change"] = "Keep a | b\r\nand verify"
        record["evidence"] = ["first | proof", "second\nproof"]
        record.update(record_update)
        rows.append(
            {
                "id": record["id"],
                "cells": [record["id"], "stale", "stale", "Verify"],
                "source_ids": ["source:" + record["id"]],
            }
        )
    return records, rows


def _duplicate_record(metadata, records, rows, handoff, notes):
    """Repeat a finding record."""
    metadata["review_findings"].append(copy.deepcopy(records[0]))


def _duplicate_row(metadata, records, rows, handoff, notes):
    """Repeat a handoff row."""
    rows.append(copy.deepcopy(rows[0]))


def _missing_row(metadata, records, rows, handoff, notes):
    """Drop a handoff row."""
    rows.pop()


def _changed_status(metadata, records, rows, handoff, notes):
    """Change a row status the helper must not decide."""
    rows[0]["cells"][-1] = "Verify"


def _second_table(metadata, records, rows, handoff, notes):
    """Append a second table to the notes."""
    notes.write_text(notes.read_text() + "\nOther evidence\n| Extra | Table |\n", encoding="utf-8")


def _second_section(metadata, records, rows, handoff, notes):
    """Repeat the whole action section in the notes."""
    notes.write_text(notes.read_text() * 2, encoding="utf-8")


def _authored_details(metadata, records, rows, handoff, notes):
    """Add an authored details section the helper does not own."""
    handoff["presentation_version"] = 7
    notes.write_text(
        notes.read_text().replace("| Required |", "| Required | ")
        + "\n## Finding Details\n\nKeep authored evidence.\n",
        encoding="utf-8",
    )


class TestReviewActionDerivation:
    """Protect the review action table derived from authored findings."""

    @pytest.mark.parametrize(
        ("metadata_update", "record_update"),
        [
            pytest.param({}, {}, id="unattributed"),
            pytest.param(
                {"reviewer_assessments": []},
                {"authors": ["Main reviewer (parent substitute)", "QA specialist"]},
                id="attributed",
            ),
        ],
    )
    def test_preserves_judgment_and_escaped_content(
        self, tmp_path: Path, metadata_update: dict, record_update: dict
    ) -> None:
        """Findings and blockers retain identities, status and source bindings while canonical text is escaped.

        Records carry pipes and CRLF; the derived notes table must escape them, keep text before and after the table,
        accept the result in the action-table validator, and be byte-stable when derived a second time.
        """
        metadata = _metadata()
        metadata.update(metadata_update)
        records, rows = _review_records_and_rows(metadata, record_update)
        handoff = {"presentation_version": 7, "tables": [{"heading": "Review Findings and Merge Blocks", "rows": rows}]}
        notes = tmp_path / "review-notes.md"
        _notes(notes, [record["id"] for record in records])
        prefix, suffix = (
            "## Decision Summary\r\n\r\nKeep this.\r\n",
            "\r\nComment after table.\r\n## Evidence\r\nUntouched.\r\n",
        )
        notes.write_bytes((prefix + notes.read_text().replace("Required |", "Verify |") + suffix).encode())
        original_metadata = copy.deepcopy(metadata)

        HELPER._derive_review_actions(tmp_path, metadata, handoff)

        text = notes.read_text()
        assert all(
            record[field].replace("\n", "<br>").replace("|", r"\|") in text
            for record in records
            for field in ("title", "summary", "closure_evidence")
            if record.get(field)
        )
        _load_validator()._validate_action_table(notes, _result(), metadata, "pr")
        assert metadata == original_metadata
        assert notes.read_bytes().startswith(prefix.encode())
        assert notes.read_bytes().endswith(suffix.encode())
        assert "parent substitute" not in text
        assert [(row["id"], row["source_ids"], row["cells"]) for row in rows] == [
            (
                record["id"],
                ["source:" + record["id"]],
                [record["id"], record["required_change"], "; ".join(record["evidence"]), "Verify"],
            )
            for record in records
        ]
        assert all(
            row.get(field) == record.get(field)
            for row, record in zip(rows, records, strict=True)
            for field in ("title", "summary", "closure_evidence", "authors")
        )
        once = notes.read_bytes()
        HELPER._derive_review_actions(tmp_path, metadata, handoff)
        assert notes.read_bytes() == once

    @pytest.mark.parametrize(
        "defect",
        [
            pytest.param(_duplicate_record, id="duplicate-record"),
            pytest.param(_duplicate_row, id="duplicate-row"),
            pytest.param(_missing_row, id="missing-row"),
            pytest.param(_changed_status, id="status"),
            pytest.param(_second_table, id="second-table"),
            pytest.param(_second_section, id="second-section"),
            pytest.param(_authored_details, id="authored-details"),
        ],
    )
    def test_rejects_ambiguous_authored_input(self, tmp_path: Path, defect: Callable) -> None:
        """Never repair identity/status decisions or erase an ambiguous notes table.

        Each case makes the authored handoff or notes ambiguous (repeated or missing identities, a decided status, extra
        tables or sections); derivation must raise and leave the notes bytes untouched.
        """
        metadata = _metadata()
        records = metadata["review_findings"] + metadata["operational_blockers"]
        rows = [{"id": record["id"], "cells": [record["id"], "stale", "stale", "Required"]} for record in records]
        handoff = {"tables": [{"heading": "Review Findings and Merge Blocks", "rows": rows}]}
        notes = tmp_path / "review-notes.md"
        _notes(notes, [record["id"] for record in records])
        defect(metadata, records, rows, handoff, notes)
        before = notes.read_bytes()

        with pytest.raises(HELPER.DeriveError, match=r"review-(action-|report-details-unowned)"):
            HELPER._derive_review_actions(tmp_path, metadata, handoff)

        assert notes.read_bytes() == before


class TestFinalizePromotion:
    """Protect one-call finalization of remediation runs from judgement-only drafts."""

    @pytest.mark.parametrize("version", [True, 0, 2, "1", None])
    def test_publication_contract_version_requires_supported_integer(self, tmp_path: Path, version: object) -> None:
        """Reject unsupported versions and JSON values that only compare equal to the version integer."""
        result = {"status": "pass", "metadata": {"mode": "pr", "worktree_publication_contract_version": version}}

        with pytest.raises(SystemExit, match="code-remediate-publication-contract-version-invalid"):
            VALIDATOR._validate_remediation_publication(result, [], tmp_path, candidate=False)

    @pytest.mark.parametrize(
        "filename",
        [
            "worktree-delivery.json",
            "publication.json",
            "publication-offer.json",
            "publication-attempt.json",
            "publication-executed",
            "publication-execution-result.json",
            "publication-readback.json",
        ],
    )
    def test_lingering_sidecar_identifies_new_passing_result(self, tmp_path: Path, filename: str) -> None:
        """Demand a binding for promoted success while preserving preliminary and failed workflows."""
        (tmp_path / filename).touch()
        result = {"status": "pass", "metadata": {"mode": "pr"}}

        with pytest.raises(SystemExit, match="code-remediate-publication-binding-required"):
            VALIDATOR._validate_remediation_publication(result, [], tmp_path, candidate=False)

        VALIDATOR._validate_remediation_publication(result, [], tmp_path, candidate=True)
        VALIDATOR._validate_remediation_publication({**result, "status": "fail"}, [], tmp_path, candidate=False)

    def test_pr_worktree_cannot_promote_without_publication_disposition(
        self,
        valid_run: tuple[Path, dict[str, object]],
        tmp_path_factory: pytest.TempPathFactory,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Block promotion before a successful linked delivery has a publication disposition."""
        run, result = valid_run
        result["metadata"]["mode"] = "pr"
        _write_worktree_delivery(run)
        assert not (run / "result.json").exists()

        exit_code, summary = _finalize_judgement_drafts(run, result, tmp_path_factory.mktemp("drafts"), "codex", capsys)

        assert exit_code == 1
        assert summary["promoted"] is False
        assert summary["steps"][-1]["code"] == "publication-disposition-required"
        assert not (run / "result.json").exists()

    def test_promotes_local_pr_worktree_with_skipped_publication(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Keep local delivery successful and render the machine-derived optional publication evidence."""
        result_path = _write_remediation_candidate(tmp_path, "code")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        pr_dir = tmp_path / "pr"
        pr_dir.mkdir()
        relevance = {
            "evaluated": True,
            "connected_items_marked_out_of_scope": 0,
            "connected_open_items_total": 0,
            "connected_selectable_items_total": 0,
            "connected_required_followup_total": 0,
        }
        result["metadata"].update(
            mode="pr",
            pr_relevance=relevance,
            merge_resolution={
                "artifact_path": str(pr_dir / "merge-resolution.json"),
                "authorization": "not-required",
                "conflicts_detected": False,
                "status": "not-needed",
            },
        )
        selection_path = tmp_path / "selection.json"
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        selection["pr_relevance"] = relevance
        selection_path.write_text(json.dumps(selection), encoding="utf-8")
        (tmp_path / "resolution-scope.md").write_bytes(HELPER.final_handoff.render_selection(selection).encode())
        with (tmp_path / "action-items.md").open("a", encoding="utf-8") as stream:
            stream.write("\n## PR Relevance Summary\n\nNo open selected items are PR-connected.\n")
        _write_pr_run_artifacts(pr_dir, "a" * 40, "b" * 40)
        (tmp_path / "merge-prestage.md").write_text(
            "\n".join(
                f"## {heading}\n\nRecorded evidence."
                for heading in (
                    "PR And Target Refresh",
                    "Clean PR Implementation Context",
                    "Target Branch Context",
                    "Conflict Risk",
                    "Resolution Strategy",
                    "Merge Execution",
                )
            ),
            encoding="utf-8",
        )
        delivery = _write_worktree_delivery(tmp_path)
        _write_publication_offer(tmp_path, delivery)
        terminal = {"schema_version": 1, "status": "skipped", "reason": "No eligible commits.", "observed_oid": None}
        evidence = json.dumps(terminal).encode()

        drafts = tmp_path_factory.mktemp("drafts")
        metadata, handoff = drafts / "metadata.json", drafts / "handoff.json"
        draft = {**result["metadata"], "worktree_publication_contract_version": 99, "worktree_publication": {}}
        metadata.write_text(json.dumps(draft), encoding="utf-8")
        handoff.write_text(json.dumps(_draft_handoff(tmp_path)), encoding="utf-8")
        argv = ["finalize", "--run", str(tmp_path), "--metadata", str(metadata), "--handoff", str(handoff)]
        argv += [
            "--status",
            "pass",
            "--confidence",
            str(result["confidence"]),
            "--artifact-path",
            result["artifact_path"],
        ]
        assert HELPER.main(argv) == 0
        assert json.loads(capsys.readouterr().out)["promoted"] is False
        preliminary_result = (tmp_path / "result.candidate.json").read_bytes()
        preliminary_handoff = (tmp_path / "final-handoff.json").read_bytes()
        preliminary_render = (tmp_path / "final.md").read_bytes()
        preliminary_validation = (tmp_path / "final-handoff.validation.json").read_bytes()
        assert "worktree_publication_contract_version" not in json.loads(preliminary_result)["metadata"]
        assert "worktree_publication" not in json.loads(preliminary_result)["metadata"]
        (tmp_path / "publication-offer.json").unlink()
        assert HELPER.main(["publication-resume", "--run", str(tmp_path)]) == 0
        resume = json.loads(capsys.readouterr().out)
        assert resume["publication"]["status"] == "skipped"
        assert resume["evidence"] == "worktree-delivery.json"
        assert resume["needs_terminal_record"] is True
        (tmp_path / "publication.json").write_bytes(evidence)
        locator = tmp_path / "remediation-worktree.json"
        locator_bytes = locator.read_bytes()
        locator.unlink()
        assert HELPER.main([*argv, "--promote"]) == 1
        blocked = json.loads(capsys.readouterr().out)
        assert blocked["promoted"] is False
        assert blocked["steps"][-1]["code"] == "publication-evidence-missing:remediation-worktree.json"
        assert not (tmp_path / "result.json").exists()
        locator.write_bytes(locator_bytes)

        exit_code = HELPER.main([*argv, "--promote"])
        summary = json.loads(capsys.readouterr().out)

        assert summary["promoted"], summary
        assert exit_code == 0
        published = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        assert published["status"] == "pass"
        assert published["metadata"]["worktree_publication"] == {
            **terminal,
            "evidence": "publication.json",
            "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
        }
        rendered = (tmp_path / "final.md").read_text(encoding="utf-8")
        assert f"[PR publication: skipped](<{tmp_path.as_posix()}/publication.json>)" in rendered
        VALIDATOR.validate("code-remediate", tmp_path, tmp_path / "result.json")
        (tmp_path / "publication.json").write_bytes(evidence + b"\n")
        with pytest.raises(SystemExit, match="code-remediate-publication-binding-mismatch"):
            VALIDATOR.validate("code-remediate", tmp_path, tmp_path / "result.json")

        published["metadata"].pop("worktree_publication")
        (tmp_path / "result.json").write_text(json.dumps(published), encoding="utf-8")
        (tmp_path / "worktree-delivery.json").unlink()
        with pytest.raises(SystemExit, match="code-remediate-publication-binding-required"):
            VALIDATOR.validate("code-remediate", tmp_path, tmp_path / "result.json")

        assert published["metadata"]["worktree_publication_contract_version"] == 1
        (tmp_path / "publication.json").unlink()
        published["metadata"].pop("worktree_publication_contract_version")
        (tmp_path / "result.json").write_text(json.dumps(published), encoding="utf-8")
        with pytest.raises(SystemExit, match="code-remediate-publication-binding-required"):
            VALIDATOR.validate("code-remediate", tmp_path, tmp_path / "result.json")
        (tmp_path / "result.json").write_text(json.dumps({**published, "schema_version": 1}), encoding="utf-8")
        with pytest.raises(SystemExit, match="code-remediate-publication-binding-required"):
            VALIDATOR.validate("code-remediate", tmp_path, tmp_path / "result.json")

        # A historical local handoff still has the locator and Markdown delivery, with no publication evidence.
        (tmp_path / "final-handoff.json").write_bytes(preliminary_handoff)
        (tmp_path / "final.md").write_bytes(preliminary_render)
        (tmp_path / "final-handoff.validation.json").write_bytes(preliminary_validation)
        (tmp_path / "result.json").write_bytes(preliminary_result)
        VALIDATOR.validate("code-remediate", tmp_path, tmp_path / "result.json")

        marker_only = json.loads(preliminary_result)
        marker_only["metadata"]["worktree_publication_contract_version"] = 1
        (tmp_path / "result.json").write_text(json.dumps(marker_only), encoding="utf-8")
        with pytest.raises(SystemExit, match="code-remediate-publication-binding-required"):
            VALIDATOR.validate("code-remediate", tmp_path, tmp_path / "result.json")

    @pytest.mark.parametrize("owner", ["user", "codex"])
    def test_promotes_judgement_drafts_on_historical_selection(
        self,
        valid_run: tuple[Path, dict[str, object]],
        tmp_path_factory: pytest.TempPathFactory,
        capsys: pytest.CaptureFixture[str],
        owner: str,
    ) -> None:
        """Derive, render, write, validate, and promote in one call when only judgement fields were authored.

        The drafts omit the handoff verification, confidence, artifacts, tables, and source records, and the metadata
        omits its handoff binding and derived copies; one finalize call must still produce a validator-accepted result.
        """
        run, result = valid_run

        exit_code, summary = _finalize_judgement_drafts(run, result, tmp_path_factory.mktemp("drafts"), owner, capsys)

        _assert_promoted_for_owner(run, exit_code, summary, owner)

    @pytest.mark.parametrize("owner", ["user", "codex"])
    def test_promotes_judgement_drafts_on_current_selection(
        self,
        valid_run: tuple[Path, dict[str, object]],
        tmp_path_factory: pytest.TempPathFactory,
        capsys: pytest.CaptureFixture[str],
        owner: str,
    ) -> None:
        """Promote judgement-only drafts on a current selection and require the latest handoff presentation.

        The run uses a version-five selection with resolution proposals; finalize must emit the current presentation
        version and render the proposal link target; a version-six handoff must be refused as stale.
        """
        run, result = valid_run
        _close_items_for_current_selection(run, result)

        exit_code, summary = _finalize_judgement_drafts(run, result, tmp_path_factory.mktemp("drafts"), owner, capsys)

        _assert_promoted_for_owner(run, exit_code, summary, owner)
        handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
        assert handoff["presentation_version"] == 15
        assert ": src/package/parser.py" in (run / "resolution-scope.md").read_text(encoding="utf-8")
        handoff["presentation_version"] = 6
        promoted = json.loads((run / "result.json").read_text(encoding="utf-8"))
        with pytest.raises(SystemExit, match="remediation-current-handoff-presentation-v8-required"):
            VALIDATOR._validate_code_remediate_final_handoff(promoted, handoff, candidate=True)

    @pytest.mark.parametrize("owner", ["user", "codex"])
    def test_promotes_genuine_blocker(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], owner: str) -> None:
        """Promote a failed real fixture while retaining the owning recovery step at the end.

        A run ends unresolved with a failing test gate, a blocked commit disposition and a user or codex owner on the
        remaining item; finalize must still promote it, validate it, and end the final document on the owner's action.
        """
        run = tmp_path / "run"
        run.mkdir()
        result_path = _write_remediation_candidate(run, "code", ("unresolved", "Blocked: regression remains."))
        result = json.loads(result_path.read_text())
        result.update(status="fail", checks_failed=["tests"])
        result["metadata"]["unresolved_summary"].update(
            selected_items_total=1,
            selected_items_unresolved=1,
            local_actionable_items_unresolved=1,
            all_local_actionable_items_closed=False,
            unresolved_reason_groups=[
                {
                    "reason": "local-code-or-doc",
                    "count": 1,
                    "owner": owner,
                    "next_action": "Fix the regression.",
                    "evidence_path": "unresolved.txt",
                }
            ],
        )
        (run / "unresolved.txt").write_text(
            "## Unresolved Work Summary\n\nOne local regression remains.\n\n"
            "## Why Selected Items Remain Unresolved\n\nClosure class: local-code-or-doc. "
            "Next owner: user or agent. Attempted evidence: test failure.\n\n## Next Action\n\nFix the regression.\n"
        )
        gates_path = run / "gates.json"
        gates = json.loads(gates_path.read_text())
        gates.update(status="fail", checks_failed=["tests"])
        gates["checks"][3].update(status="fail", exit_code=1, reason="Regression remains.")
        gates_path.write_text(json.dumps(gates))
        handoff = _draft_handoff(run)
        handoff["remaining"][0].update(owner=owner, next_action="Fix the regression.")
        handoff["commit_disposition"] = {
            "status": "blocked",
            "reason": "Regression remains.",
            "evidence": "unresolved.txt",
        }
        (tmp_path / "metadata.json").write_text(json.dumps(_stale_metadata(result)))
        (tmp_path / "handoff.json").write_text(json.dumps(handoff))
        argv = [
            "finalize",
            "--run",
            str(run),
            "--metadata",
            str(tmp_path / "metadata.json"),
            "--handoff",
            str(tmp_path / "handoff.json"),
            "--status",
            "fail",
            "--confidence",
            str(result["confidence"]),
            "--artifact-path",
            result["artifact_path"],
            "--promote",
        ]
        code = HELPER.main(argv)
        summary = json.loads(capsys.readouterr().out)
        assert (code, summary["status"], summary["promoted"]) == (0, "pass", True), summary
        VALIDATOR.validate("code-remediate", run, run / "result.json")
        final = (run / "final.md").read_text()
        assert final.index("**Artifact**") < final.index("**Recommendations / next steps**")
        assert final.endswith(f"| {owner} | Boundary guard | Fix the regression. |\n")


class TestAuditFinalization:
    """Exercise audit bookkeeping recovery through the ordinary CLI and artifact consumer."""

    @pytest.mark.integration
    @pytest.mark.parametrize("alter_evidence", [False, True])
    def test_finalizes_only_retained_audit_evidence(self, audit_run: tuple, alter_evidence: bool) -> None:  # noqa: F811
        """Recover omitted copies while preserving authored decisions and rejecting changed cost evidence.

        The audit draft supplies findings and metadata but omits generated handoff fields. The CLI must promote a valid
        result accepted by the next validator; altered behavioral evidence must remain rejected and unpromoted.
        """
        _, run, result = audit_run
        metadata = copy.deepcopy(result["metadata"])
        metadata["authored_finding"] = {"summary": "A scoped finding remains.", "decision": "Keep for owner."}
        handoff = {
            "schema_version": 1,
            "skill": "audit",
            "branch": "standard",
            "outcome": {"title": "Audit", "summary": "Assessment accepted; one scoped finding remains."},
            "tables": [
                {
                    "heading": "Results",
                    "columns": ["Item", "Severity / impact", "Decision", "Evidence", "Next action"],
                    "rows": [
                        {
                            "id": "finding",
                            "cells": [
                                "Scoped finding",
                                "medium",
                                "Keep",
                                "audit-ledger.md",
                                "Restore the missing scope check.",
                            ],
                            "source_ids": ["finding"],
                        }
                    ],
                }
            ],
            "source_records": [{"id": "finding", "evidence": "audit-ledger.md"}],
            "source_coverage": {
                "source_records_total": 1,
                "represented_source_records_total": 1,
                "omitted_source_records_total": 0,
            },
            "remaining": [
                {
                    "row_id": "finding",
                    "item": "Scoped finding",
                    "owner": "maintainer",
                    "next_action": "Restore the missing scope check.",
                }
            ],
            "next_steps": ["finding"],
            "caller_contract": None,
        }
        (run / "metadata-draft.json").write_text(json.dumps(metadata), encoding="utf-8")
        (run / "handoff-draft.json").write_text(json.dumps(handoff), encoding="utf-8")
        if alter_evidence:
            (run / "behavior.json").write_text('{"critical_regressions": 1}', encoding="utf-8")
        command = [
            sys.executable,
            str(PLUGIN_ROOT / "shared/remediation_finalize.py"),
            "finalize",
            "--skill",
            "audit",
            "--run",
            str(run),
            "--metadata",
            str(run / "metadata-draft.json"),
            "--handoff",
            str(run / "handoff-draft.json"),
            "--status",
            "pass",
            "--confidence",
            "0.95",
            "--medium",
            "1",
            "--artifact-path",
            str(run / "result.json"),
            "--promote",
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        assert completed.returncode == int(alter_evidence), completed.stdout + completed.stderr
        summary = json.loads(completed.stdout)
        assert summary["promoted"] is not alter_evidence
        if alter_evidence:
            assert not (run / "result.json").exists()
            assert any("audit-cost-evidence-digest-mismatch" in error["code"] for error in summary["errors"])
            return
        promoted = json.loads((run / "result.json").read_bytes())
        assert promoted["findings"]["medium"] == 1
        assert {key: promoted["metadata"][key] for key in metadata} == metadata
        assert json.loads((run / "final-handoff.json").read_bytes())["tables"] == handoff["tables"]
        consumer = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "shared/validate-artifacts.py"),
                "--skill",
                "audit",
                "--out",
                str(run),
                "--result",
                str(run / "result.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert consumer.returncode == 0, consumer.stdout + consumer.stderr
