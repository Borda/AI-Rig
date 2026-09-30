"""Prove complete bounded source delivery and aggregate native admission at the ordinary consumer."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

import test_review_prepare as preparation


def _batch_command(run: Path, operation: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the installed-path batch command with synthetic native receipts."""
    args = [sys.executable, str(preparation.HELPER), operation, "--out", str(run)]
    if operation == "prepare":
        root = json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
        args.extend(["--run-id", "bounded-review", "--parent-thread-id", "parent", "--source-root", root, "--batches"])
    else:
        args.extend(["--codex-home", str(home)])
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=False)


def _batch_inputs(tmp_path: Path, *, large: bool = False, untracked: bool = False, deleted: bool = False) -> Path:
    """Declare complete changed and unchanged source selection without copying the source into briefs."""
    run = preparation._review_inputs(
        tmp_path, second_file=True, unchanged_caller=True, large_source=large, untracked=untracked, deleted=deleted
    )
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing.update(independent_review_required=False, independence_requirement_evidence=None)
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    for brief in briefs.values():
        brief["source_paths"] = ["widget.py", "other.py", "stable.py", *(["new.txt"] if untracked else [])]
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    return run


def _batch_finding(identity: str, severity: str, summary: str) -> dict[str, object]:
    """Declare one concrete fixture obligation with programmatically bound source coordinates."""
    return {
        "id": identity,
        "severity": severity,
        "title": summary,
        "summary": summary,
        "required_change": summary,
        "closure_evidence": "An executable check confirms the required behavior.",
        "evidence": [{"path": "widget.py", "start_line": 1, "end_line": 1}],
    }


def _batch_output(records: list[dict[str, object]], rating: int, disposition: str | None = None) -> str:
    """Render the actual batch client response with explicit inventory and optional disposition."""
    return (
        "## Reviewer Findings\n```json\n"
        + json.dumps(records)
        + "\n```\n\n"
        + ("## Finding Dispositions\n" + disposition + "\n\n" if disposition else "")
        + '## Reviewer Confidence\n```json\n{"score": 0.95, "scope": "Frozen source and declared interactions.", "gaps": [{"gap": "Synthetic offline client.", "status": "unresolved", "rationale": "Fixture proves admission without a live semantic reviewer."}]}\n```\n\n'
        + f"## Reviewer Assessment\nRating: {rating}\nRationale: Assessment accounts for every declared obligation."
    )


def _completed_batches(
    tmp_path: Path,
    *,
    blocker: tuple[int, str] | None = None,
    large: bool = False,
    disposition: str | None = None,
    single_file: bool = False,
    source_rating: int = 3,
    interaction_finding: tuple[int, str, int] | None = None,
    final_finding: tuple[str, int] | None = None,
    advisor: str | None = None,
    interaction_records: list[dict[str, object]] | None = None,
    source_records: list[dict[str, object]] | None = None,
    final_records: list[dict[str, object]] | None = None,
    final_dispositions: dict[str, str] | None = None,
    five_roles: bool = False,
) -> tuple[Path, Path]:
    """Record actual context-reader bytes for every source wave and final interaction wave."""
    run = (
        preparation._five_role_review_inputs(tmp_path)
        if five_roles
        else preparation._review_inputs(tmp_path)
        if single_file
        else _batch_inputs(tmp_path, large=large)
    )
    if advisor:
        routing = json.loads((run / "review-routing.json").read_text(encoding="utf-8"))
        routing["signals"]["axis_" + advisor.replace("-", "_")] = True
        routing["triggered_roles"].append(advisor)
        routing["trigger_reasons"][advisor] = ["Explicit advisory selection."]
        routing["sol_selection"] = {
            advisor: {
                "source": "explicit-user-selection",
                "parent_event_id": "user-selection",
                "selection_sha256": "a" * 64,
            }
        }
        (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8", newline="\n")
        briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
        briefs[advisor] = {**briefs["qa-specialist"], "axis": "Explicit advisory"}
        (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    # Separate source waves without expensive large native fixture transcripts.
    (run / "challenger-evidence.md").write_text("Bounded source review.\n" * 3500, encoding="utf-8", newline="\n")
    result = _batch_command(run, "prepare")
    assert result.returncode == 0, result.stderr
    schedule = json.loads((run / "batch-dispatch.json").read_text(encoding="utf-8"))
    assert len(schedule["waves"]) > 1
    home = tmp_path / "codex-home"
    for wave in schedule["waves"]:
        directory = run / wave["directory"]
        preparation._assembly_evidence(
            tmp_path,
            prepared_run=directory,
            home=home,
            wave_index=wave["wave"],
            final_header="missing",
            active_limit=4 if five_roles else None,
            findings={
                blocker[1]: _batch_output(
                    source_records
                    if source_records is not None
                    else [
                        _batch_finding(
                            "F_SOURCE", "low" if source_rating == 2 else "high", "Correct the broken caller contract."
                        )
                    ],
                    source_rating,
                )
            }
            if blocker and blocker[0] == wave["wave"]
            else None,
        )
        result = _batch_command(directory, "assemble-wave", home)
        assert result.returncode == 0, result.stderr
    inventory = json.loads((run / "batch-inventory.json").read_text(encoding="utf-8"))
    lengths = {item["path"]: len(item["content"].splitlines()) for item in inventory["source_snapshot"]["files"]}
    briefs = {}
    for role in json.loads((run / "review-briefs.json").read_text(encoding="utf-8")):
        path = f"{role}-interactions.md"
        (run / path).write_text(
            "Inspect changed widget producer with other consumer and retained findings.\n",
            encoding="utf-8",
            newline="\n",
        )
        briefs[role] = {
            "axis": "Producer and consumer interactions",
            "evidence_path": path,
            "source_paths": [{"path": name, "start_line": 1, "end_line": count} for name, count in lengths.items()],
            "paired_ranges": [
                [{"path": path, "start_line": 1, "end_line": 1} for path in ("widget.py", "other.py", "stable.py")],
                [
                    {"path": "widget.py", "start_line": 1, "end_line": 1},
                    {"path": "widget.py", "start_line": lengths["widget.py"], "end_line": lengths["widget.py"]},
                ],
            ],
        }
    if single_file:
        for brief in briefs.values():
            brief["source_paths"] = [{"path": "widget.py", "start_line": 1, "end_line": lengths["widget.py"]}]
            brief.pop("paired_ranges")
    (run / "interaction-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _batch_command(run, "prepare-interactions", home)
    assert result.returncode == 0, result.stderr
    interaction_schedule = json.loads((run / "interaction-dispatch.json").read_text(encoding="utf-8"))
    for index, wave in enumerate(interaction_schedule["waves"], len(schedule["waves"]) + 1):
        directory = run / wave["directory"]
        findings = None
        if interaction_finding and index - len(schedule["waves"]) == interaction_finding[0]:
            role, rating = interaction_finding[1:]
            findings = {
                role: _batch_output(
                    interaction_records
                    if interaction_records is not None
                    else [
                        _batch_finding(
                            "F_INTERACTION",
                            "low" if rating == 2 else "high",
                            "Correct the required caller interaction.",
                        )
                    ],
                    rating,
                )
            }
        if blocker and disposition:
            plan = json.loads((directory / "inspection-plan.json").read_text(encoding="utf-8"))
            identity = f"source-{blocker[0]:03d}.{blocker[1]}.F_SOURCE"
            for entry in plan["contexts"]:
                context = (directory / entry["context_path"]).read_text(encoding="utf-8")
                if f"Source finding ID: {identity}\n" in context:
                    findings = {
                        entry["role_id"]: _batch_output(
                            [], 1, f"Source disposition {identity}: rejected; Evidence: {disposition}"
                        )
                    }
                    break
        preparation._assembly_evidence(
            tmp_path,
            prepared_run=directory,
            home=home,
            wave_index=index,
            final_header="missing",
            active_limit=4 if five_roles else None,
            findings=findings,
        )
        result = _batch_command(directory, "assemble-wave", home)
        assert result.returncode == 0, result.stderr
    result = _batch_command(run, "prepare-consolidation", home)
    assert result.returncode == 0, result.stderr
    interactions = run / "batches/interactions"
    preparation._assembly_evidence(
        tmp_path,
        prepared_run=interactions,
        home=home,
        wave_index=len(schedule["waves"]) + len(interaction_schedule["waves"]) + 1,
        final_header="missing",
        active_limit=4 if five_roles else None,
        findings={
            final_finding[0]: _batch_output(
                final_records
                if final_records is not None
                else [
                    _batch_finding(
                        "F_FINAL", "low" if final_finding[1] == 2 else "high", "Correct the final required contract."
                    )
                ],
                final_finding[1],
            )
        }
        if final_finding
        else {role: _batch_output([], 1, statement) for role, statement in final_dispositions.items()}
        if final_dispositions
        else None,
    )
    result = _batch_command(interactions, "assemble-wave", home)
    assert result.returncode == 0, result.stderr
    return run, home


def test_batches_reconstruct_large_complete_unicode_source(tmp_path: Path) -> None:
    """Full source beyond the old limit is delivered, including unchanged caller bytes."""
    run = _batch_inputs(tmp_path, large=True)
    before = preparation._prepare(run)
    assert before.returncode != 0
    assert "review-context-capacity-exceeded" in before.stderr

    result = _batch_command(run, "prepare")

    assert result.returncode == 0, result.stderr
    inventory = json.loads((run / "batch-inventory.json").read_text(encoding="utf-8"))
    assert inventory["wave_count"] > 4
    for role, entries in inventory["segments"].items():
        parts = []
        offset = 0
        for entry in entries:
            content = (
                run / "batches" / f"source-{entry['wave']:03d}" / "specialists" / f"{role}-context.md"
            ).read_bytes()
            assert len(content) <= 65536
            content.decode("utf-8")
            assert entry["start"] == offset
            assert (
                "Frozen source coordinates: " + json.dumps(entry["locations"], ensure_ascii=False, sort_keys=True)
            ).encode() in content
            parts.append(content[entry["payload_offset"] :])
            offset = entry["end"]
        reconstructed = b"".join(parts)
        assert reconstructed == (run / "source-contexts" / f"{role}.md").read_bytes()
        source = next(
            record["content"] for record in inventory["source_snapshot"]["files"] if record["path"] == "widget.py"
        )
        assert source.count("unchanged = 'café'\n") == 18000
        assert source.encode("utf-8") in reconstructed
        assert b"caller = 5\n" in reconstructed
        assert any(
            location["path"] == "widget.py" and location["start_line"] > 1
            for entry in entries[1:]
            for location in entry["locations"]
        )
    for wave in json.loads((run / "batch-dispatch.json").read_text(encoding="utf-8"))["waves"]:
        assert 1 <= len(wave["calls"]) <= 4
        assert len({call["role"] for call in wave["calls"]}) == len(wave["calls"])


@pytest.mark.parametrize("route", ["deletion", "untracked"])
def test_batches_keep_deletion_and_untracked_evidence(tmp_path: Path, route: str) -> None:
    """Batch preparation preserves both missing tip source and files without tracked hunks."""
    run = _batch_inputs(tmp_path, untracked=route == "untracked", deleted=route == "deletion")
    result = _batch_command(run, "prepare")
    assert result.returncode == 0, result.stderr
    context = (run / "source-contexts/challenger.md").read_text(encoding="utf-8")
    if route == "deletion":
        assert "widget.py (missing" in context
        assert "deleted file mode" in context
    else:
        assert "new_value = 7\n" in context
        assert "Untracked file: exact source bytes" in context


def test_aggregate_admits_every_native_wave_and_interaction_output(tmp_path: Path) -> None:
    """Ordinary manifest admission validates all successful batch receipts, not a selected batch."""
    run, home = _completed_batches(tmp_path)
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 7
    assert manifest["manifest_kind"] == "batched-review"
    spec = importlib.util.spec_from_file_location(
        "batch_acceptance_validator", preparation.SKILL / "validate_artifacts.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    by_role = validator._validate_manifest_entries(
        run,
        manifest,
        manifest["passes"],
        {"challenger", "qa-specialist"},
        home,
        "parent",
        tmp_path,
        require_role_card_receipts=True,
    )
    assert set(by_role) == {"challenger", "qa-specialist"}
    retained = validator._validate_manifest_entries(
        run,
        manifest,
        manifest["passes"],
        {"challenger", "qa-specialist"},
        home,
        "parent",
        tmp_path,
        retained_role_cards=True,
        require_role_card_receipts=True,
    )
    assert retained == by_role
    summary = validator._validate_review_runtime(run, manifest, manifest["passes"], home, "parent")
    assert summary["actual_mode"] == "parallel"
    assert summary["batch_mode"] == "serial-waves"
    assert summary["batch_count"] > 1


@pytest.mark.parametrize(
    "problem", ["missing-wave", "segment-drift", "source-drift", "missing-interactions", "lost-output"]
)
def test_aggregate_rejects_incomplete_source_or_interaction_evidence(tmp_path: Path, problem: str) -> None:
    """No absent batch, stale source, or dropped original finding can produce accepted coverage."""
    run, home = _completed_batches(tmp_path)
    if problem == "missing-wave":
        (run / "batches/source-001/specialist-manifest.json").unlink()
    elif problem == "segment-drift":
        path = run / "batches/source-001/specialists/challenger-context.md"
        path.write_bytes(path.read_bytes() + b"forged")
    elif problem == "source-drift":
        root = json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
        (Path(root) / "widget.py").write_text("value = 99\n", encoding="utf-8", newline="\n")
    elif problem == "missing-interactions":
        (run / "batches/interactions/specialist-manifest.json").unlink()
    else:
        path = run / "batches/interactions/specialists/challenger-context.md"
        before = path.read_bytes()
        assert b"The inspected scope is clean." in before
        path.write_bytes(before.replace(b"The inspected scope is clean.", b"Finding ignored.", 1))
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode != 0
    assert not (run / "specialist-manifest.json").exists()


@pytest.mark.parametrize("header", ["missing", "truncated"])
def test_native_seven_accepts_receipts_without_copied_final_header(tmp_path: Path, header: str) -> None:
    """Audited reads bind the exact input even when a model omits or mistypes copied metadata."""
    run, home, children = preparation._assembly_evidence(tmp_path, final_header=header)
    result = preparation._assemble(run, home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_kind"] == "native-wave"
    for item in manifest["passes"]:
        terminal = json.loads(children[item["role"]].read_text(encoding="utf-8").splitlines()[-1])["payload"][
            "last_agent_message"
        ]
        attempt = item["attempts"][0]
        assert (run / attempt["raw_output_path"]).read_bytes() == terminal.encode("utf-8")
        assert (run / item["output_path"]).read_bytes() == (terminal.strip() + "\n").encode("utf-8")


def test_schema_six_still_rejects_truncated_final_header(tmp_path: Path) -> None:
    """Historical admission remains strict rather than silently weakening an old artifact family."""
    run, home, _ = preparation._assembly_evidence(tmp_path, final_header="truncated")
    assert preparation._assemble(run, home).returncode == 0
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 6
    spec = importlib.util.spec_from_file_location(
        "legacy_header_validator", preparation.SKILL / "validate_artifacts.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    with pytest.raises(SystemExit, match="provenance-output-header-mismatch"):
        validator._validate_manifest_entries(
            run, manifest, manifest["passes"], {"challenger", "qa-specialist"}, home, "parent", tmp_path
        )


def test_challenge_preflight_admits_native_seven_single_reviewer(tmp_path: Path) -> None:
    """The single-challenger consumer accepts the new native producer's fully bound wave."""
    run, home = _completed_batches(tmp_path)
    directory = run / "batches/source-002"
    manifest = json.loads((directory / "specialist-manifest.json").read_text(encoding="utf-8"))
    assert [item["role"] for item in manifest["passes"]] == ["challenger"]
    result = subprocess.run(
        [
            sys.executable,
            str(preparation.SKILL / "validate_artifacts.py"),
            "--out",
            str(directory),
            "--manifest-only",
            "--challenge-only",
            "--codex-home",
            str(home),
            "--parent-thread-id",
            "parent",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("problem", ["raw-output", "normalized-output", "input", "reader", "role", "extra-read"])
def test_native_seven_rejects_tampering_despite_missing_final_header(tmp_path: Path, problem: str) -> None:
    """Derived provenance never accepts forged outputs, input identity, reader, or native page receipts."""
    run, home, children = preparation._assembly_evidence(tmp_path, final_header="missing")
    assert preparation._assemble(run, home).returncode == 0
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    item = manifest["passes"][0]
    if problem in {"raw-output", "normalized-output"}:
        key = "raw_output_path" if problem == "raw-output" else "output_path"
        path = run / item["attempts"][0][key]
        path.write_bytes(path.read_bytes() + b"forged")
    elif problem == "input":
        manifest["review_input_sha256"] = "a" * 64
    elif problem == "reader":
        manifest["context_reader_sha256"] = "a" * 64
    elif problem == "role":
        item["role"] = "doc-scribe"
    else:
        path = children[item["role"]]
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        receipt = next(row for row in rows if row["payload"].get("type") == "custom_tool_call_output")
        rows.insert(-1, receipt)
        preparation._write_jsonl(path, rows)
    spec = importlib.util.spec_from_file_location(
        "native_tamper_validator", preparation.SKILL / "validate_artifacts.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    with pytest.raises(SystemExit):
        validator._validate_manifest_entries(
            run, manifest, manifest["passes"], {"challenger", "qa-specialist"}, home, "parent", tmp_path
        )


@pytest.mark.parametrize(
    "blocker",
    [pytest.param((1, "qa-specialist"), id="first-other-role"), pytest.param((2, "challenger"), id="later-wave")],
)
def test_result_rejects_lost_unresolved_source_finding(tmp_path: Path, blocker: tuple[int, str]) -> None:
    """A later clean assessment cannot silently close an original source blocker."""
    run, home = _completed_batches(tmp_path, blocker=blocker)
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    unresolved = [item for item in manifest["source_findings"] if item["disposition"] == "unresolved"]
    assert len(unresolved) == 1
    assert unresolved[0]["pass"]["role"] == blocker[1]
    assert unresolved[0]["manifest_path"].startswith(f"batches/source-{blocker[0]:03d}/")
    spec = importlib.util.spec_from_file_location(
        "source_result_validator", preparation.SKILL / "validate_artifacts.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    metadata = {"source_findings": manifest["source_findings"], "review_decision": {"recommendation": "accept-as-is"}}
    with pytest.raises(SystemExit, match="review-batch-source-findings-unresolved"):
        validator._validate_batch_source_findings({"status": "pass", "metadata": metadata, "findings": {}}, manifest)
    metadata["review_decision"]["recommendation"] = "needs-more-work"
    metadata["review_findings"] = [
        {
            "id": "F_CANONICAL",
            "severity": "high",
            **{key: value for key, value in unresolved[0]["original"].items() if key not in {"id", "evidence"}},
            "evidence": [unresolved[0]["manifest_path"], "widget.py:1-1"],
            "authors": [validator._readable_review_role(unresolved[0]["pass"]["role"])],
        }
    ]
    metadata["source_finding_mapping"] = {unresolved[0]["finding_id"]: ["F_CANONICAL"]}
    validator._validate_batch_source_findings(
        {"status": "fail", "metadata": metadata, "findings": {"high": 1}}, manifest
    )
    metadata["source_finding_mapping"][unresolved[0]["finding_id"]] = []
    with pytest.raises(SystemExit, match="review-batch-result-source-findings-dropped"):
        validator._validate_batch_source_findings({"status": "fail", "metadata": metadata, "findings": {}}, manifest)


@pytest.mark.parametrize("role", ["security-auditor", "solution-architect"])
def test_batch_wave_preserves_explicit_advisor_selection(tmp_path: Path, role: str) -> None:
    """Both explicit advisor roles retain authorization through native wave admission."""
    run = _batch_inputs(tmp_path)
    routing = json.loads((run / "review-routing.json").read_text(encoding="utf-8"))
    routing["signals"]["axis_" + role.replace("-", "_")] = True
    routing["triggered_roles"].append(role)
    routing["trigger_reasons"][role] = ["Explicit advisory selection."]
    selection = {
        role: {"source": "explicit-user-selection", "parent_event_id": "user-selection", "selection_sha256": "a" * 64}
    }
    routing["sol_selection"] = selection
    (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs[role] = {**briefs["challenger"], "axis": "Explicit advisory"}
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _batch_command(run, "prepare")
    assert result.returncode == 0, result.stderr
    directory = run / "batches/source-001"
    home = tmp_path / "codex-home"
    preparation._assembly_evidence(tmp_path, prepared_run=directory, home=home, wave_index=1, final_header="missing")
    result = _batch_command(directory, "assemble-wave", home)
    assert result.returncode == 0, result.stderr
    assert (
        json.loads((directory / "specialist-manifest.json").read_text(encoding="utf-8"))["sol_selection"] == selection
    )


@pytest.mark.parametrize("gap", ["unchanged-caller", "split-file"])
def test_interactions_reject_missing_frozen_source_obligation(tmp_path: Path, gap: str) -> None:
    """Every unchanged caller and cross-segment file interval is required for interaction admission."""
    run = _batch_inputs(tmp_path, large=gap == "split-file")
    assert _batch_command(run, "prepare").returncode == 0
    spec = importlib.util.spec_from_file_location(
        "interaction_obligation_batches", preparation.SKILL / "review_batches.py"
    )
    batches = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(batches)
    inventory = batches.validate_inventory(run)
    paths = [
        {"path": "widget.py", "start_line": 1, "end_line": 1},
        {"path": "other.py", "start_line": 1, "end_line": 1},
    ]
    if gap == "split-file":
        paths.append({"path": "stable.py", "start_line": 1, "end_line": 1})
    briefs = {
        role: {"axis": "All interactions", "evidence_path": role + "-evidence.md", "source_paths": paths}
        for role in inventory["selections"]
    }
    with pytest.raises(ValueError, match="review-interaction-unreviewed-source"):
        batches.interaction_contexts(run, inventory, briefs, b"")


def test_large_interactions_complete_bounded_serial_native_waves(tmp_path: Path) -> None:
    """A real large source scope completes interaction batches without relocating the old capacity failure."""
    run, home = _completed_batches(tmp_path, large=True)
    schedule = json.loads((run / "interaction-dispatch.json").read_text(encoding="utf-8"))
    assert len(schedule["waves"]) > 4
    for wave in schedule["waves"]:
        plan = json.loads((run / wave["directory"] / "inspection-plan.json").read_text(encoding="utf-8"))
        assert 1 <= len(plan["contexts"]) <= 4
        for entry in plan["contexts"]:
            context = (run / wave["directory"] / entry["context_path"]).read_bytes()
            assert len(context) <= 65536
            context.decode("utf-8")
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "evidence",
    [
        "widget.py:1-1 - Existing behavior: frozen value already satisfies the original source contract",
        "widget.py:999-999 - Existing behavior: frozen value already satisfies the original source contract",
        "widget.py:1-1 - Applied a new fix after review",
    ],
)
def test_source_disposition_requires_receipt_bound_existing_evidence(tmp_path: Path, evidence: str) -> None:
    """Only an independently read exact source interval can support source finding rejection."""
    run, home = _completed_batches(tmp_path, blocker=(1, "qa-specialist"), disposition=evidence)
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    finding = next(
        item for item in manifest["source_findings"] if item["finding_id"] == "source-001.qa-specialist.F_SOURCE"
    )
    assert finding["original"]["summary"] == "Correct the broken caller contract."
    if evidence.startswith("widget.py:1-1 - Existing behavior:"):
        assert finding["disposition"] == "rejected"
        assert (
            finding["disposition_evidence"]["attempt"]["agent_thread_id"]
            != finding["pass"]["attempts"][0]["agent_thread_id"]
        )
    else:
        assert finding["disposition"] == "unresolved"
        assert "disposition_evidence" not in finding


def test_interaction_fragment_coordinates_cover_single_long_unicode_line() -> None:
    """Later fragments remain inspectable when one source line exceeds the entire context bound."""
    spec = importlib.util.spec_from_file_location("fragment_coordinates", preparation.SKILL / "review_batches.py")
    batches = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(batches)
    content = ("café" * 40000 + "\n").encode()
    fragments = batches._bounded_fragments(content, 16000, "source module.py:1-1", 0)
    payloads = []
    offset = 0
    for fragment in fragments:
        assert len(fragment) <= 16000
        fragment.decode()
        header, payload = fragment.split(b"complete evidence SHA-256 " + batches._digest(content).encode() + b"\n", 1)
        assert b"Origin: source module.py:1-1" in header
        assert f"UTF-8 byte interval: [{offset}, {offset + len(payload)})".encode() in header
        offset += len(payload)
        payloads.append(payload)
    assert b"".join(payloads) == content


def test_single_file_interaction_native_admission(tmp_path: Path) -> None:
    """One file requires full intra-file inspection without an invented cross-file pair."""
    run, home = _completed_batches(tmp_path, single_file=True)
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr


def test_large_source_and_patch_fragments_keep_diff_coordinates(tmp_path: Path) -> None:
    """A middle-of-hunk fragment retains file identity, exact diff intervals, and original hunk header."""
    run = preparation._review_inputs(tmp_path, large_source=True, large_patch=True)
    result = _batch_command(run, "prepare")
    assert result.returncode == 0, result.stderr
    inventory = json.loads((run / "batch-inventory.json").read_text(encoding="utf-8"))
    patch = (run / "diff.patch").read_bytes()
    assert len(patch) > 65536
    mid_hunk = []
    for entries in inventory["segments"].values():
        for entry in entries:
            for location in entry["locations"]:
                if location.get("kind") == "diff" and location["start_byte"] > 1000:
                    assert location["path"] == "widget.py"
                    assert location["hunk_header"].startswith("@@")
                    assert location["start_line"] > 1
                    assert location["end_byte"] > location["start_byte"]
                    mid_hunk.append(location)
    assert mid_hunk


def test_low_source_finding_requires_canonical_inventory_despite_zero_blockers(tmp_path: Path) -> None:
    """A minor source finding survives a later clean interaction assessment and remains remediable."""
    run, home = _completed_batches(tmp_path, blocker=(1, "qa-specialist"), source_rating=2)
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    finding = next(
        item for item in manifest["source_findings"] if item["finding_id"] == "source-001.qa-specialist.F_SOURCE"
    )
    assert finding["pass"]["blocking_findings"] == 0
    assert finding["original"]["severity"] == "low"
    assert finding["disposition"] == "unresolved"
    spec = importlib.util.spec_from_file_location("minor_source_result", preparation.SKILL / "validate_artifacts.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    metadata = {
        "source_findings": manifest["source_findings"],
        "review_decision": {"recommendation": "minor-changes"},
        "review_findings": [],
    }
    result = {"status": "pass", "metadata": metadata, "findings": {"low": 1}}
    with pytest.raises(SystemExit, match="review-batch-result-source-finding-inventory-dropped"):
        validator._validate_batch_source_findings(result, manifest)
    metadata["source_finding_mapping"] = {finding["finding_id"]: ["F_MINOR"]}
    metadata["review_findings"] = [
        {
            "id": "F_MINOR",
            "severity": "low",
            **{key: value for key, value in finding["original"].items() if key not in {"id", "evidence"}},
            "evidence": [finding["manifest_path"], "widget.py:1-1"],
            "authors": [validator._readable_review_role(finding["pass"]["role"])],
        }
    ]
    validator._validate_batch_source_findings(result, manifest)


def test_mixed_source_findings_keep_each_original_severity() -> None:
    """A blocking source pass never forces a separate minor source finding into a blocking severity."""
    spec = importlib.util.spec_from_file_location("mixed_source_findings", preparation.SKILL / "validate_artifacts.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    ledger = [
        {
            "finding_id": "source-001.challenger",
            "disposition": "unresolved",
            "original": _batch_finding("F_BLOCK", "high", "Original blocking producer finding."),
            "pass": {"blocking_findings": 1, "role": "challenger"},
            "manifest_path": "batches/source-001/specialist-manifest.json",
        },
        {
            "finding_id": "source-002.qa-specialist",
            "disposition": "unresolved",
            "original": _batch_finding("F_MINOR", "low", "Original minor caller finding."),
            "pass": {"blocking_findings": 0, "role": "qa-specialist"},
            "manifest_path": "batches/source-002/specialist-manifest.json",
        },
    ]
    records = [
        {
            "id": "F_BLOCK",
            "severity": "high",
            **{key: value for key, value in ledger[0]["original"].items() if key not in {"id", "evidence"}},
            "evidence": [ledger[0]["manifest_path"], "widget.py:1-1"],
            "authors": [validator._readable_review_role(ledger[0]["pass"]["role"])],
        },
        {
            "id": "F_MINOR",
            "severity": "low",
            **{key: value for key, value in ledger[1]["original"].items() if key not in {"id", "evidence"}},
            "evidence": [ledger[1]["manifest_path"], "widget.py:1-1"],
            "authors": [validator._readable_review_role(ledger[1]["pass"]["role"])],
        },
    ]
    metadata = {
        "source_findings": ledger,
        "source_finding_mapping": {ledger[0]["finding_id"]: ["F_BLOCK"], ledger[1]["finding_id"]: ["F_MINOR"]},
        "review_findings": records,
        "review_decision": {"recommendation": "needs-more-work"},
    }
    validator._validate_batch_source_findings(
        {"status": "fail", "metadata": metadata}, {"manifest_kind": "batched-review", "source_findings": ledger}
    )


@pytest.mark.parametrize(
    "phase,role,rating",
    [
        pytest.param("interaction", "qa-specialist", 3, id="intermediate-other-role-blocker"),
        pytest.param("interaction", "challenger", 2, id="intermediate-minor"),
        pytest.param("final", "challenger", 3, id="final-blocker"),
        pytest.param("source", "challenger", 3, id="original-later-source-blocker"),
        pytest.param("interaction-later", "challenger", 3, id="later-intermediate-sibling-blocker"),
    ],
)
def test_complete_result_rejects_lost_findings_from_every_successful_phase(
    tmp_path: Path, phase: str, role: str, rating: int
) -> None:
    """Complete canonical result admission retains intermediate and final findings despite clean later output."""
    run, home = _completed_batches(
        tmp_path,
        interaction_finding=(2 if phase == "interaction-later" else 1, role, rating)
        if phase.startswith("interaction")
        else None,
        final_finding=(role, rating) if phase == "final" else None,
        blocker=(2, role) if phase == "source" else None,
        large=phase == "interaction-later",
    )
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    identity = (
        {
            "interaction": "interaction-001",
            "interaction-later": "interaction-002",
            "source": "source-002",
            "final": "interactions",
        }[phase]
        + "."
        + role
    )
    identity += "." + {"source": "F_SOURCE", "final": "F_FINAL"}.get(phase, "F_INTERACTION")
    record = next((item for item in manifest["source_findings"] if item["finding_id"] == identity), None)
    assert record is not None, "successful interaction/final pass omitted from canonical ledger"
    assert record["disposition"] == "unresolved"
    assert record["original"]["severity"] == ("low" if rating == 2 else "high")
    spec = importlib.util.spec_from_file_location("all_phase_validator", preparation.SKILL / "validate_artifacts.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    metadata = {
        "scope": "working-tree",
        "risk_tier": "HIGH_RISK",
        "finding_records_version": 1,
        "review_findings": [],
        "operational_blockers": [],
        "review_decision": {
            "recommendation": "accept-as-is",
            "summary": "Clean review.",
            "rationale": "Final assessment omitted earlier findings.",
        },
        "reviewer_assessments": [
            {"role": validator._readable_review_role(item["role"]), "rating": 1, "evidence": item["output_path"]}
            for item in manifest["passes"]
        ],
        "specialist_passes": manifest["passes"],
        "specialist_manifest": "specialist-manifest.json",
        "source_findings": manifest["source_findings"],
        "source_finding_mapping": {},
        "execution_mode": summary["actual_mode"],
        "execution_evidence_level": summary["evidence_level"],
        "write_parallel_eligible": False,
        "execution_observed_controls": summary["observed_controls"],
        "review_run_id": manifest["review_run_id"],
        "review_input_sha256": manifest["review_input_sha256"],
        "independence_requirement_evidence": None,
        "fanout_substituted": False,
        "independence_satisfied": True,
        "independence_required": False,
        "confidence_gaps": ["Synthetic offline receipts."],
        "confidence_gap_closures": [
            {
                "gap": "Synthetic offline receipts.",
                "status": "unresolved",
                "rationale": "Fixture does not make paid calls.",
            }
        ],
        "confidence_recovery": {
            "initial_confidence": 0.95,
            "final_confidence": 0.95,
            "status": "fair",
            "evidence": ["Native fixture receipts validated."],
            "recovery_actions": ["Checked every frozen wave."],
            "remaining_limits": ["Synthetic offline receipts."],
        },
    }
    candidate = {
        "schema_version": 3,
        "status": "pass",
        "confidence": 0.95,
        "checks_failed": [],
        "checks_run": [],
        "findings": dict.fromkeys(validator.FINDING_SEVERITIES, 0),
        "metadata": metadata,
    }
    (run / "review-notes.md").write_text(
        "\n\n".join(f"## {section}\n\nFixture assessment." for section in validator.REQUIRED_SECTIONS),
        encoding="utf-8",
        newline="\n",
    )
    path = run / "result.json"
    path.write_text(json.dumps(candidate), encoding="utf-8", newline="\n")
    with pytest.raises(SystemExit, match="review-batch-source-findings-unresolved"):
        validator._validate_result(run, path, home, "parent", tmp_path)
    spec = importlib.util.spec_from_file_location(
        "all_phase_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    with pytest.raises(LookupError, match="review-batch-source-findings-unresolved"):
        finder.complete_review_run(run, codex_home=home, parent_thread_id="parent")
    with pytest.raises(LookupError, match="review-batch-source-findings-unresolved"):
        finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")


@pytest.mark.parametrize("role", ["security-auditor", "solution-architect"])
def test_all_wave_selection_subsets_retain_global_advisory_authority(tmp_path: Path, role: str) -> None:
    """Unequal source and interaction contexts admit exact advisory subsets through final aggregation."""
    run, home = _completed_batches(tmp_path, advisor=role)
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    global_selection = json.loads((run / "review-routing.json").read_text(encoding="utf-8"))["sol_selection"]
    assert manifest["sol_selection"] == global_selection
    assert (
        json.loads((run / "batch-inventory.json").read_text(encoding="utf-8"))["plan"]["sol_selection"]
        == global_selection
    )
    saw_short_wave = False
    for reference in manifest["batch_execution"]["waves"]:
        wave = json.loads((run / reference["manifest_path"]).read_text(encoding="utf-8"))
        roles = {item["role"] for item in wave["passes"]}
        assert wave.get("sol_selection") == (global_selection if role in roles else None)
        saw_short_wave |= role not in roles
    assert saw_short_wave, "unequal contexts must exercise a wave without the authorized advisory role"


def test_batch_admission_rejects_prose_only_two_minor_findings(tmp_path: Path) -> None:
    """Two prose obligations cannot become an invented empty or pass-level batch inventory."""
    run = _batch_inputs(tmp_path)
    assert _batch_command(run, "prepare").returncode == 0
    directory = run / "batches/source-001"
    home = tmp_path / "codex-home"
    preparation._assembly_evidence(
        tmp_path,
        prepared_run=directory,
        home=home,
        wave_index=1,
        final_header="missing",
        findings={
            "qa-specialist": "F_ONE: Document the returned value.\nF_TWO: Correct the independent empty-input explanation.\n\n## Reviewer Assessment\n\nRating: 2\nRationale: Both minor findings remain open."
        },
    )
    result = _batch_command(directory, "assemble-wave", home)
    assert result.returncode != 0
    assert "review-batch-individual-findings" in result.stderr
    assert not (directory / "specialist-manifest.json").exists()


def _canonical_report(
    run: Path,
    records: list[dict[str, object]],
    mapping: dict[str, list[str]],
    recommendation: str,
    status: str = "pass",
    duplicates: dict[str, str] | None = None,
) -> Path:
    """Construct complete ordinary result, gates and bound grouped handoff for real consumer admission."""
    spec = importlib.util.spec_from_file_location(
        "individual_report_validator", preparation.SKILL / "validate_artifacts.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    spec = importlib.util.spec_from_file_location(
        "individual_report_handoff", preparation.SKILL.parents[1] / "shared/final_handoff.py"
    )
    final_handoff = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = final_handoff
    spec.loader.exec_module(final_handoff)
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    metadata = {
        "scope": "working-tree",
        "risk_tier": "HIGH_RISK",
        "finding_records_version": 1,
        "review_findings": records,
        "operational_blockers": [],
        "review_decision": {
            "recommendation": recommendation,
            "summary": "Review selected canonical findings.",
            "rationale": "Retain mapped findings and their existing evidence.",
        },
        "reviewer_assessments": [
            {
                "role": validator._readable_review_role(item["role"]),
                "rating": validator._retained_reviewer_rating(
                    run / item["output_path"], local_reviewer_wave=False, main=False, role=item["role"]
                ),
                "evidence": item["output_path"],
            }
            for item in manifest["passes"]
        ],
        "specialist_passes": manifest["passes"],
        "specialist_manifest": "specialist-manifest.json",
        "source_findings": manifest["source_findings"],
        "source_finding_mapping": mapping,
        "source_finding_duplicates": duplicates or {},
        "execution_mode": summary["actual_mode"],
        "execution_evidence_level": summary["evidence_level"],
        "write_parallel_eligible": False,
        "execution_observed_controls": summary["observed_controls"],
        "review_run_id": manifest["review_run_id"],
        "review_input_sha256": manifest["review_input_sha256"],
        "independence_requirement_evidence": None,
        "fanout_substituted": False,
        "independence_satisfied": True,
        "independence_required": False,
        "confidence_gaps": ["Synthetic offline receipts."],
        "confidence_gap_closures": [
            {
                "gap": "Synthetic offline receipts.",
                "status": "unresolved",
                "rationale": "Probe uses retained local fixture receipts.",
            }
        ],
        "confidence_recovery": {
            "initial_confidence": 0.95,
            "final_confidence": 0.95,
            "status": "fair",
            "evidence": ["Frozen wave receipts validated."],
            "recovery_actions": ["Revalidated all constituent waves."],
            "remaining_limits": ["Synthetic offline receipts."],
        },
    }
    result_path = run / "result.json"
    gate_ids = ["lint", "format", "types", "tests", "review"]
    result = {
        "schema_version": 3,
        "status": status,
        "confidence": 0.95,
        "checks_failed": [],
        "checks_run": gate_ids,
        "findings": {
            severity: sum(record["severity"] == severity for record in records)
            for severity in validator.FINDING_SEVERITIES
        },
        "metadata": metadata,
        "artifact_path": str(result_path),
        "recommendations": [],
        "follow_up": [],
    }
    notes = "\n\n".join(f"## {section}\n\nFixture assessment." for section in validator.REQUIRED_SECTIONS)
    if records:
        notes += "\n\n## Review Findings and Merge Blocks\n\n| Finding / area | Author | Required change | Evidence | Status |\n| --- | --- | --- | --- | --- |\n"
        notes += "".join(
            f"| {record['id']} | {', '.join(record['authors'])} | {record['required_change']} | {'; '.join(record['evidence'])} | Open |\n"
            for record in records
        )
    (run / "review-notes.md").write_text(notes + "\n", encoding="utf-8", newline="\n")
    checks = []
    for gate in gate_ids:
        for suffix in ("command", "stdout", "stderr"):
            (run / f"{gate}.{suffix}.txt").write_text("", encoding="utf-8", newline="\n")
        check = {
            "id": gate,
            "status": "not-applicable",
            "exit_code": 0,
            "duration_seconds": 0.0,
            "command_path": f"{gate}.command.txt",
            "stdout": f"{gate}.stdout.txt",
            "stderr": f"{gate}.stderr.txt",
            "reason": "Synthetic minimal source declares no command for this gate.",
        }
        if gate == "review":
            check["status"] = "pass"
            check.pop("reason")
            (run / check["stdout"]).write_text(
                "Canonical assessment admission exercised independently.\n", encoding="utf-8", newline="\n"
            )
        checks.append(check)
    (run / "gates.json").write_text(
        json.dumps({"status": "pass", "checks_failed": [], "checks": checks}), encoding="utf-8", newline="\n"
    )
    fields = [
        ("Scope", "working-tree"),
        ("Revision", "diff sha256:" + metadata["review_input_sha256"]),
        ("CI", "unavailable"),
        ("Type", "fix"),
        ("Suggestion", "minor changes" if recommendation == "minor-changes" else "needs work"),
    ]
    source_records = [{"id": "snapshot:" + field, "evidence": "review-notes.md"} for field, _ in fields]
    tables = [
        {
            "heading": "Review Snapshot",
            "columns": ["Field", "Value"],
            "reviewers": metadata["reviewer_assessments"],
            "summary": metadata["review_decision"]["summary"],
            "rows": [
                {"id": "S" + str(i), "cells": list(pair), "source_ids": ["snapshot:" + pair[0]]}
                for i, pair in enumerate(fields)
            ],
        }
    ]
    if records:
        source_records += [{"id": record["id"], "evidence": "review-notes.md"} for record in records]
        tables += [
            {
                "heading": "Review Findings and Merge Blocks",
                "layout": "grouped",
                "columns": ["Finding / area", "Required change", "Evidence", "Status"],
                "rows": [
                    {
                        "id": record["id"],
                        "cells": [record["id"], record["required_change"], "; ".join(record["evidence"]), "Open"],
                        "source_ids": [record["id"]],
                        "authors": record["authors"],
                        "title": record["title"],
                        "summary": record["summary"],
                        "closure_evidence": record["closure_evidence"],
                    }
                    for record in records
                ],
            }
        ]
    handoff = {
        "schema_version": 1,
        "presentation_version": 3,
        "skill": "code-review",
        "branch": "assessed",
        "outcome": {"title": "Review Decision", "summary": "Recommendation: " + recommendation + "."},
        "tables": tables,
        "source_records": source_records,
        "source_coverage": {
            "source_records_total": len(source_records),
            "represented_source_records_total": len(source_records),
            "omitted_source_records_total": 0,
        },
        "verification": [
            {"check": check["id"], "status": check["status"], "evidence": check["stdout"]} for check in checks
        ],
        "remaining": [],
        "next_steps": [],
        "confidence": {
            "score": 0.95,
            "band": "fair",
            "limits": metadata["confidence_recovery"]["remaining_limits"],
            "gaps": metadata["confidence_gap_closures"],
        },
        "artifacts": [{"label": "Result", "path": str(result_path)}],
        "caller_contract": None,
    }
    handoff_path = run / "final-handoff.json"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8", newline="\n")
    validation = final_handoff.render_files(handoff_path, run / "final.md", run / "final-handoff.validation.json")
    metadata["final_handoff"] = {
        "schema_version": 1,
        "handoff_path": str(handoff_path),
        "handoff_sha256": validation["handoff_sha256"],
        "rendered_path": str(run / "final.md"),
        "rendered_sha256": validation["rendered_sha256"],
        "validation_path": str(run / "final-handoff.validation.json"),
        "branch": "assessed",
    }
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    return result_path


@pytest.mark.parametrize(
    "phase,role",
    [
        pytest.param("interaction", "qa-specialist", id="original-two-minors"),
        pytest.param("source", "challenger", id="source-sibling"),
        pytest.param("final", "qa-specialist", id="final-sibling"),
    ],
)
def test_every_individual_minor_survives_complete_consumers(tmp_path: Path, phase: str, role: str) -> None:
    """Omission of either independent obligation rejects; complete concrete actions reach actual intake."""
    one = _batch_finding("F_ONE", "low", "Document the returned value.")
    two = _batch_finding("F_TWO", "low", "Correct the independent empty-input explanation.")
    run, home = _completed_batches(
        tmp_path,
        blocker=(1, role) if phase == "source" else None,
        source_rating=2,
        source_records=[one, two] if phase == "source" else None,
        interaction_finding=(1, role, 2) if phase == "interaction" else None,
        interaction_records=[one, two] if phase == "interaction" else None,
        final_finding=(role, 2) if phase == "final" else None,
        final_records=[one, two] if phase == "final" else None,
    )
    assert _batch_command(run, "assemble-batches", home).returncode == 0
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    originals = manifest["source_findings"]
    assert len(originals) == 2
    assert {item["original"]["id"] for item in originals} == {"F_ONE", "F_TWO"}
    assert all(item["original"]["evidence"][0]["source_sha256"] for item in originals)
    spec = importlib.util.spec_from_file_location(
        "individual_consumer_validator", preparation.SKILL / "validate_artifacts.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    spec = importlib.util.spec_from_file_location(
        "individual_consumer_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    records = [
        {
            **item["original"],
            "evidence": [
                item["manifest_path"],
                *[
                    f"{entry['path']}:{entry['start_line']}-{entry['end_line']}"
                    for entry in item["original"]["evidence"]
                ],
            ],
            "authors": [validator._readable_review_role(role)],
        }
        for item in originals
    ]
    path = _canonical_report(run, records[:1], {originals[0]["finding_id"]: [records[0]["id"]]}, "minor-changes")
    with pytest.raises(SystemExit, match="review-batch-result-source-finding-inventory-dropped"):
        validator._validate_result(run, path, home, "parent", tmp_path)
    with pytest.raises(LookupError, match="review-batch-result-source-finding-inventory-dropped"):
        finder.complete_review_run(run, codex_home=home, parent_thread_id="parent")
    with pytest.raises(LookupError, match="review-batch-result-source-finding-inventory-dropped"):
        finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")
    # Complete accounting preserves exact original claims, required changes and closure evidence.
    path = _canonical_report(
        run, records, {item["finding_id"]: [item["original"]["id"]] for item in originals}, "minor-changes"
    )
    validator._validate_result(run, path, home, "parent", tmp_path)
    for problem in ("author", "coordinate"):
        altered = [dict(record) for record in records]
        if problem == "author":
            altered[0]["authors"] = [
                validator._readable_review_role("challenger" if role != "challenger" else "qa-specialist")
            ]
        else:
            altered[0]["evidence"] = [originals[0]["manifest_path"]]
        bad = _canonical_report(
            run, altered, {item["finding_id"]: [item["original"]["id"]] for item in originals}, "minor-changes"
        )
        with pytest.raises(LookupError, match="review-batch-result-source-finding-inventory-dropped"):
            finder.require_assessed_review_result(bad, codex_home=home, parent_thread_id="parent")
    path = _canonical_report(
        run, records, {item["finding_id"]: [item["original"]["id"]] for item in originals}, "minor-changes"
    )
    final = finder.complete_review_run(run, codex_home=home, parent_thread_id="parent")
    assert b"F_ONE" in final and b"F_TWO" in final
    assert finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent") == path


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate-id",
        "duplicate-key",
        "unbound-path",
        "model-hash",
        "empty-claim",
        "prose-outside",
        "wrong-blocker-count",
        "container-severity",
        "confidence-missing",
        "confidence-score",
        "confidence-gap",
    ],
)
def test_batch_raw_inventory_rejects_malformed_or_unbound_obligations(tmp_path: Path, problem: str) -> None:
    """Receipt-bound parser refuses ambiguous records, unseen source coordinates and invented counts."""
    run = _batch_inputs(tmp_path)
    assert _batch_command(run, "prepare").returncode == 0
    directory, home = run / "batches/source-001", tmp_path / "codex-home"
    record = _batch_finding("F_ONE", "low", "Document the returned value.")
    records = [record, record] if problem == "duplicate-id" else [record]
    if problem == "unbound-path":
        record["evidence"] = [{"path": "not-frozen.py", "start_line": 1, "end_line": 1}]
    elif problem == "model-hash":
        record["evidence"] = [{"path": "widget.py", "start_line": 1, "end_line": 1, "source_sha256": "a" * 64}]
    elif problem == "empty-claim":
        record["summary"] = ""
    elif problem == "container-severity":
        record["severity"] = []
    output = _batch_output(records, 2)
    if problem == "confidence-missing":
        start = output.index("## Reviewer Confidence")
        end = output.index("## Reviewer Assessment")
        output = output[:start] + output[end:]
    elif problem == "confidence-score":
        output = output.replace('"score": 0.95', '"score": []')
    elif problem == "confidence-gap":
        output = output.replace('"status": "unresolved"', '"status": []')
    elif problem == "duplicate-key":
        output = output.replace('"id": "F_ONE"', '"id": "F_ONE", "id": "F_LOST"')
    elif problem == "prose-outside":
        output = "F_EXTRA: Correct an independent caller defect.\n" + output
    preparation._assembly_evidence(
        tmp_path,
        prepared_run=directory,
        home=home,
        wave_index=1,
        final_header="missing",
        findings={"qa-specialist": output},
        blocking_counts={"qa-specialist": 1 if problem == "wrong-blocker-count" else 0},
    )
    result = _batch_command(directory, "assemble-wave", home)
    assert result.returncode != 0
    assert "review-batch-individual-findings" in result.stderr
    assert not (directory / "specialist-manifest.json").exists()


@pytest.mark.parametrize("case", ["supported", "self", "wrong-id", "missing-range", "unsupported-fix"])
def test_intermediate_disposition_uses_later_independent_frozen_source(tmp_path: Path, case: str) -> None:
    """An admitted intermediate false claim closes only with exact later independent source witness."""
    identity = "interaction-001.qa-specialist.F_INTERACTION"
    witness_id = "interaction-999.qa-specialist.F_INTERACTION" if case == "wrong-id" else identity
    evidence = "widget.py:999-999" if case == "missing-range" else "widget.py:1-1"
    rationale = (
        "Applied a new fix after review"
        if case == "unsupported-fix"
        else "False positive: frozen source actually returns value two rather than value one"
    )
    role = "qa-specialist" if case == "self" else "challenger"
    run, home = _completed_batches(
        tmp_path,
        interaction_finding=(1, "qa-specialist", 3),
        final_dispositions={role: f"Source disposition {witness_id}: rejected; Evidence: {evidence} - {rationale}"},
    )
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    original = next(item for item in manifest["source_findings"] if item["finding_id"] == identity)
    assert original["disposition"] == ("rejected" if case == "supported" else "unresolved")
    context = (run / f"batches/interactions/specialists/{role}-context.md").read_text(encoding="utf-8")
    if case == "supported":
        assert f"Source finding ID: {identity}\n" in context
        assert "value = 2\n" in context
        assert "### widget.py:1-1, source SHA-256 " in context
        assert original["disposition_evidence"]["role"] == "challenger"


@pytest.mark.parametrize("case", ["exact", "missing-origins", "distinct"])
def test_individual_duplicate_actions_require_exact_obligations_and_origins(tmp_path: Path, case: str) -> None:
    """Shared canonical actions retain every origin and cannot collapse distinct same-location claims."""
    first = _batch_finding("F_SHARED", "low", "Document the returned value.")
    second = _batch_finding(
        "F_SHARED",
        "low",
        "Document the returned value." if case != "distinct" else "Correct the independent empty-input explanation.",
    )
    run, home = _completed_batches(
        tmp_path,
        blocker=(1, "challenger"),
        source_rating=2,
        source_records=[first],
        interaction_finding=(1, "qa-specialist", 2),
        interaction_records=[second],
    )
    assembled = _batch_command(run, "assemble-batches", home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    originals = manifest["source_findings"]
    assert len(originals) == 2
    assert len({item["finding_id"] for item in originals}) == 2
    ordered = sorted(originals, key=lambda item: item["finding_id"])
    record = {
        **originals[0]["original"],
        "id": "F_CANONICAL",
        "authors": ["Challenger", "QA specialist"],
        "evidence": [*[item["manifest_path"] for item in originals], "widget.py:1-1"],
    }
    duplicates = {} if case == "missing-origins" else {ordered[1]["finding_id"]: ordered[0]["finding_id"]}
    path = _canonical_report(
        run,
        [record],
        {item["finding_id"]: ["F_CANONICAL"] for item in originals},
        "minor-changes",
        duplicates=duplicates,
    )
    spec = importlib.util.spec_from_file_location(
        "duplicate_consumer_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    if case == "exact":
        assert finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent") == path
        rendered = finder.complete_review_run(run, codex_home=home, parent_thread_id="parent")
        assert b"widget.py:1-1" in rendered and b"Challenger" in rendered and b"QA specialist" in rendered
    else:
        with pytest.raises(
            LookupError,
            match="review-batch-(duplicate-accounting-mismatch|result-source-finding-inventory-dropped|distinct-findings-collapsed)",
        ):
            finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")


def test_optional_disposition_witness_over_capacity_keeps_actionable_finding(tmp_path: Path) -> None:
    """A large optional dismissal witness leaves the obligation open without blocking the bounded final wave."""
    record = _batch_finding("F_WIDE", "low", "Document the full selected source behavior.")
    record["evidence"] = [{"path": "widget.py", "start_line": 1, "end_line": 18001}]
    run, home = _completed_batches(
        tmp_path, large=True, interaction_finding=(1, "qa-specialist", 2), interaction_records=[record]
    )
    assembled = _batch_command(run, "assemble-batches", home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    original = next(item for item in manifest["source_findings"] if item["original"]["id"] == "F_WIDE")
    assert original["disposition"] == "unresolved"
    context = (run / "batches/interactions/specialists/challenger-context.md").read_bytes()
    assert len(context) <= 65536
    assert f"Source finding ID: {original['finding_id']}\n".encode() not in context
    action = {
        **original["original"],
        "authors": ["QA specialist"],
        "evidence": [original["manifest_path"], "widget.py:1-18001"],
    }
    path = _canonical_report(run, [action], {original["finding_id"]: ["F_WIDE"]}, "minor-changes")
    spec = importlib.util.spec_from_file_location(
        "bounded_consumer_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    assert finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent") == path


@pytest.mark.parametrize("mutation", [None, "missing", "duplicate", "allocated-five", "reverse-queue"])
def test_five_role_batches_preserve_roster_and_findings(tmp_path: Path, mutation: str | None) -> None:
    """Five mandatory roles remain admitted through all phases without lost findings or duplicate passes."""
    run, home = _completed_batches(
        tmp_path,
        five_roles=True,
        single_file=True,
        blocker=(1, "sw-engineer"),
        interaction_finding=(1, "doc-scribe", 2),
        final_finding=("data-steward", 2),
    )
    expected = {"challenger", "data-steward", "doc-scribe", "qa-specialist", "sw-engineer"}
    for schedule_name in ("batch-dispatch.json", "interaction-dispatch.json"):
        schedule = json.loads((run / schedule_name).read_text(encoding="utf-8"))
        covered = set()
        for wave in schedule["waves"]:
            directory = run / wave["directory"]
            plan = json.loads((directory / "inspection-plan.json").read_text(encoding="utf-8"))
            sizes = {entry["role_id"]: (directory / entry["context_path"]).stat().st_size for entry in plan["contexts"]}
            ordered = [
                call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
                for call in wave["calls"]
            ]
            assert ordered == sorted(sizes, key=lambda role: (-sizes[role], role))
            covered.update(sizes)
        assert covered == expected
    final = run / "batches/interactions"
    plan = json.loads((final / "inspection-plan.json").read_text(encoding="utf-8"))
    sizes = {entry["role_id"]: (final / entry["context_path"]).stat().st_size for entry in plan["contexts"]}
    dispatch = json.loads((final / "dispatch.json").read_text(encoding="utf-8"))
    assert [
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        for call in dispatch["calls"]
    ] == sorted(sizes, key=lambda role: (-sizes[role], role))
    assert set(sizes) == expected
    assembled = _batch_command(run, "assemble-batches", home)
    assert assembled.returncode == 0, assembled.stderr
    manifest_path = run / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {item["role"] for item in manifest["passes"]} == expected
    unresolved = [record for record in manifest["source_findings"] if record["disposition"] == "unresolved"]
    assert {(record["pass"]["role"], record["original"]["id"]) for record in unresolved} == {
        ("sw-engineer", "F_SOURCE"),
        ("doc-scribe", "F_INTERACTION"),
        ("data-steward", "F_FINAL"),
    }
    if mutation == "missing":
        manifest["passes"] = [item for item in manifest["passes"] if item["role"] != "data-steward"]
    elif mutation == "duplicate":
        manifest["passes"].append(manifest["passes"][0])
    if mutation in {"allocated-five", "reverse-queue"}:
        schedule = json.loads((run / "batch-dispatch.json").read_text(encoding="utf-8"))
        wave = schedule["waves"][0]
        source = run / wave["directory"]
        source_manifest = json.loads((source / "specialist-manifest.json").read_text(encoding="utf-8"))
        children = {
            item["role"]: home / "sessions" / f"rollout-{item['attempts'][0]['agent_thread_id']}.jsonl"
            for item in source_manifest["passes"]
        }
        preparation._record_native_schedule(
            source,
            home,
            children,
            scenario="spawn-all-five" if mutation == "allocated-five" else "reversed-largest-order",
            wave_index=wave["wave"],
        )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    checked = subprocess.run(
        [
            sys.executable,
            str(preparation.SKILL / "validate_artifacts.py"),
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
    if mutation:
        assert checked.returncode != 0
        if mutation in {"allocated-five", "reverse-queue"}:
            expected_error = (
                "review-inspection-active-capacity-exceeded"
                if mutation == "allocated-five"
                else "review-inspection-dispatch-order-mismatch"
            )
            assert expected_error in checked.stderr
        else:
            assert "role" in checked.stderr or "pass" in checked.stderr
    else:
        assert checked.returncode == 0, checked.stderr
