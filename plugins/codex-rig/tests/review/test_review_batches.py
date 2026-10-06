"""Prove complete bounded source delivery and aggregate native admission at the ordinary consumer."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import test_review_prepare as preparation


def test_batch_response_declares_scalar_closure_evidence() -> None:
    """Prevent the dispatch prompt from inviting array-valued closure obligations."""
    helpers = preparation.runpy.run_path(str(preparation.SKILL / "review_batches.py"))
    instruction = helpers["BATCH_FINDINGS_INSTRUCTION"].decode()
    assert "closure_evidence (one nonempty string, never an array)" in instruction


@pytest.mark.parametrize(
    "prefix",
    [
        "none",
        "exact",
        "wrong-role",
        "wrong-attempt",
        "wrong-context",
        "duplicate",
        "comment",
        "interior",
        "prose",
        "leading-newline",
        "bom",
        "trailing-prose",
        "malformed-body",
    ],
)
@pytest.mark.integration
def test_batch_native_prefix_preserves_raw_and_strict_body(tmp_path: Path, prefix: str) -> None:
    """Only a frozen-identity marker may precede an otherwise unchanged strict native batch response."""
    run = _batch_inputs(tmp_path)
    assert _batch_command(run, "prepare").returncode == 0
    wave = run / "batches/source-001"
    marker = preparation._CONTEXT_READER(wave / "inspection-plan.json", "challenger", 1, 1).splitlines()[0]
    record = _batch_finding("A", "medium", "Preserve the reviewed obligation.")
    body = _batch_output([record], 3)
    response = body if prefix == "none" else marker + "\n" + body
    if prefix == "wrong-role":
        response = response.replace("role=challenger", "role=qa-specialist", 1)
    elif prefix == "wrong-attempt":
        response = response.replace("attempt=1", "attempt=2", 1)
    elif prefix == "wrong-context":
        response = response.replace("context=", "context=invalid", 1)
    elif prefix == "duplicate":
        response = marker + "\n" + response
    elif prefix == "comment":
        response = "<!-- arbitrary comment -->\n" + body
    elif prefix == "interior":
        response = body.replace("## Reviewer Confidence", marker + "\n## Reviewer Confidence")
    elif prefix == "prose":
        response = marker + "\nExtra prose.\n" + body
    elif prefix == "leading-newline":
        response = "\n" + response
    elif prefix == "bom":
        response = "\ufeff" + response
    elif prefix == "trailing-prose":
        response += "\nExtra prose."
    elif prefix == "malformed-body":
        response = response.replace('"id": "A"', '"id": "bad.id"')
    _, home, children = preparation._assembly_evidence(
        tmp_path, prepared_run=wave, findings={"challenger": response}, blocking_counts={"challenger": 1}
    )
    before = children["challenger"].read_bytes()
    result = _batch_command(wave, "assemble-wave", home)
    if prefix in {"none", "exact"}:
        assert result.returncode == 0, result.stderr
        manifest = json.loads((wave / "specialist-manifest.json").read_bytes())
        item = next(p for p in manifest["passes"] if p["role"] == "challenger")
        attempt = item["attempts"][0]
        assert (wave / attempt["raw_output_path"]).read_bytes() == response.encode()
        assert (wave / item["output_path"]).read_bytes() == (response.strip() + "\n").encode()
        assert item["reviewer_findings"][0]["summary"] == record["summary"]
        assert item["blocking_findings"] == 1
        import review_batches
        import validate_artifacts

        review_batches.validate_wave_findings(wave, manifest, manifest["passes"])
        if prefix == "exact":
            with pytest.raises(SystemExit, match="review-batch-individual-findings-format"):
                validate_artifacts._batch_reviewer_findings(
                    wave / attempt["raw_output_path"],
                    json.loads((run / "batch-inventory.json").read_bytes())["source_snapshot"],
                    "challenger",
                )
            historical = {**manifest, "schema_version": 6}
            with pytest.raises(SystemExit, match="review-batch-individual-findings-format"):
                review_batches.validate_wave_findings(wave, historical, historical["passes"])
            for number in (1, 2):
                selected = json.loads(json.dumps(item))
                selected["selected_attempt"] = number
                selected["attempts"] = [selected["attempts"][0], {**selected["attempts"][0], "attempt": 2}]
                assert validate_artifacts._batch_provenance_header(wave, manifest, selected) == marker.replace(
                    "attempt=1", f"attempt={number}"
                )
            invalid = json.loads(json.dumps(manifest))
            invalid["review_input_sha256"] = "a" * 64
            with pytest.raises(SystemExit, match="review-batch-provenance-identity-invalid"):
                validate_artifacts._batch_provenance_header(wave, invalid, item)
    else:
        assert result.returncode != 0
        expected = (
            "review-assessment-content-invalid" if prefix == "trailing-prose" else "review-batch-individual-findings-"
        )
        assert expected in result.stderr
        assert not (wave / "specialist-manifest.json").exists()
    assert children["challenger"].read_bytes() == before


@pytest.mark.parametrize("mutation", ["none", "claim", "severity", "evidence", "second-repair"])
def test_same_wave_schema_repair_preserves_original_claims(tmp_path: Path, mutation: str) -> None:
    """Accept one independently observed shape correction and reject substantive substitutions."""
    run = _batch_inputs(tmp_path)
    assert _batch_command(run, "prepare").returncode == 0
    wave = run / "batches/source-001"
    record = _batch_finding("A", "medium", "Preserve every original obligation.")
    record["closure_evidence"] = ["Check the first invariant.", "Check the second invariant."]
    original = _batch_output([record], 3)
    _, home, children = preparation._assembly_evidence(
        tmp_path, prepared_run=wave, findings={"challenger": original}, blocking_counts={"challenger": 1}
    )
    initial = _batch_command(wave, "assemble-wave", home)
    assert initial.returncode != 0
    assert "review-batch-individual-findings-record:challenger" in initial.stderr
    command = [
        sys.executable,
        str(preparation.HELPER),
        "prepare-repair",
        "--out",
        str(wave),
        "--codex-home",
        str(home),
        "--role",
        "challenger",
        "--kind",
        "closure-evidence-shape",
    ]
    repaired = subprocess.run(command, capture_output=True, text=True, check=False)
    assert repaired.returncode == 0, repaired.stderr
    arguments = json.loads(repaired.stdout)["arguments"]
    expected = arguments["message"].split("Return exactly this validated correction:\n", 1)[1]
    corrected = json.loads(json.dumps(record))
    corrected["closure_evidence"] = "Check the first invariant.\nCheck the second invariant."
    assert expected == _batch_output([corrected], 3)
    if mutation == "claim":
        expected = expected.replace("Preserve every original obligation.", "No obligation remains.")
    elif mutation == "severity":
        expected = expected.replace('"medium"', '"low"')
    elif mutation == "evidence":
        expected = expected.replace('"start_line": 1', '"start_line": 2')
    rows = [json.loads(line) for line in children["challenger"].read_text().splitlines()]
    session = rows[0]["payload"]
    old_path = session["agent_path"]
    new_path = old_path.removesuffix("_a1") + "_a2"
    old_thread = session["id"]
    new_thread = "repair-thread"
    rows = json.loads(json.dumps(rows).replace(old_path, new_path).replace(old_thread, new_thread))
    rows = [row for row in rows if row["type"] != "response_item"]
    rows.insert(
        2,
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "content": [{"type": "encrypted_content", "encrypted_content": arguments["message"]}],
            },
        },
    )
    rows[0]["payload"]["timestamp"] = "2026-01-01T10:01:00.050Z"
    terminal = rows[-1]["payload"]
    terminal.update(started_at=1767261660.2, completed_at=1767261661.0, last_agent_message=expected)
    preparation._write_jsonl(home / f"sessions/rollout-{new_thread}.jsonl", rows)
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
                    "call_id": "spawn-repair",
                    "arguments": json.dumps(arguments),
                },
            },
            {
                "timestamp": "2026-01-01T10:01:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "spawn-repair",
                    "output": json.dumps({"task_name": new_path}),
                },
            },
            {
                "timestamp": "2026-01-01T10:01:01.100Z",
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": new_path,
                    "content": [{"type": "input_text", "text": f"Message Type: FINAL_ANSWER\nPayload:\n{expected}"}],
                },
            },
        ]
    )
    preparation._write_jsonl(parent_path, parent)
    if mutation == "second-repair":
        duplicate = home / f"sessions/rollout-duplicate-{new_thread}.jsonl"
        preparation._write_jsonl(duplicate, rows)
    assembled = _batch_command(wave, "assemble-wave", home)
    if mutation != "none":
        assert assembled.returncode != 0, assembled.stdout
        diagnostic = (
            "review-child-session-not-unique"
            if mutation == "second-repair"
            else "review-batch-individual-findings-evidence"
            if mutation == "evidence"
            else "review-repair-claims-changed"
        )
        assert diagnostic in assembled.stderr
        return
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((wave / "specialist-manifest.json").read_text())
    item = next(item for item in manifest["passes"] if item["role"] == "challenger")
    assert manifest["schema_version"] == 8
    assert item["selected_attempt"] == 2
    assert len(item["attempts"]) == 2
    assert (wave / item["attempts"][0]["raw_output_path"]).read_text() == original
    assert item["reviewer_findings"][0]["summary"] == record["summary"]
    assert item["reviewer_findings"][0]["closure_evidence"] == corrected["closure_evidence"]
    assert json.loads((wave / "inspection-summary.json").read_text())["actual_mode"] == "parallel"
    historical = {**manifest, "schema_version": 7}
    validator = preparation.runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))
    with pytest.raises(SystemExit, match="manifest-invalid-internal-recovery:challenger"):
        validator["_validate_manifest_entries"](
            wave, historical, manifest["passes"], {"challenger", "qa-specialist"}, home, "parent", tmp_path
        )


@pytest.mark.parametrize(
    "closure",
    [
        pytest.param([], id="empty-array"),
        pytest.param([""], id="empty-member"),
        pytest.param(["Check", 1], id="numeric-member"),
        "Already scalar",
    ],
)
def test_schema_repair_rejects_other_closure_shapes(closure: object) -> None:
    """Reject unrelated invalid shapes and valid source findings that need no correction."""
    validator = preparation.runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))
    record = _batch_finding("A", "medium", "Retained obligation.")
    record["closure_evidence"] = closure
    snapshot = {"files": [{"path": "widget.py", "kind": "text", "content": "value = 2\n", "sha256": "a" * 64}]}
    with pytest.raises(SystemExit, match="review-repair-(ineligible-closure|no-shape-error):challenger"):
        validator["_closure_shape_repair"](_batch_output([record], 3), snapshot, "challenger")


@pytest.mark.parametrize("redirect", ["> unauthorized-file", "< unrelated-input"])
def test_dispatch_repair_rejects_shell_redirection(tmp_path: Path, redirect: str) -> None:
    """Reject a failed dispatch that accesses unrelated files through shell redirection."""
    run = _batch_inputs(tmp_path)
    assert _batch_command(run, "prepare").returncode == 0
    wave = run / "batches/source-001"
    _, home, children = preparation._assembly_evidence(
        tmp_path,
        prepared_run=wave,
        findings={"challenger": _batch_output([], 5)},
        blocking_counts={"challenger": 0},
    )
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    tools = [
        row
        for row in rows
        if row["type"] == "response_item"
        and row["payload"].get("type") in {"custom_tool_call", "custom_tool_call_output"}
    ]
    call, output = tools[:2]
    source = call["payload"]["input"]
    arguments = json.loads(source.split("tools.exec_command(", 1)[1].split("); text", 1)[0])
    malformed = {
        **arguments,
        "cmd": arguments["cmd"].replace("review_context.py", "review_prepare.py") + " " + redirect,
    }
    call["payload"]["input"] = source.replace(
        json.dumps(arguments, ensure_ascii=False), json.dumps(malformed, ensure_ascii=False)
    )
    output["payload"]["output"] = [
        {"type": "input_text", "text": "Script completed\n"},
        {"type": "input_text", "text": "usage: review_prepare.py\nerror: unsupported arguments"},
    ]
    preparation._write_jsonl(child, [row for row in rows if row not in tools] + [call, output])
    repaired = subprocess.run(
        [
            sys.executable,
            str(preparation.HELPER),
            "prepare-repair",
            "--out",
            str(wave),
            "--codex-home",
            str(home),
            "--role",
            "challenger",
            "--kind",
            "incomplete-dispatch",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert repaired.returncode != 0, repaired.stdout
    assert "review-repair-dispatch-cause-unproven:challenger" in repaired.stderr
    assert not (wave / "repair-dispatch.challenger.json").exists()


def _verify_unchanged_source_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real source verification once per call signature for the rest of one test.

    Every aggregate validation re-verifies the checkout with dozens of Git subprocesses. A test that never touches the
    checkout after its fixtures are built repeats that work over bytes that cannot have changed; for the slowest such
    test that cut runtime by about a third. Opt in only where the source tree is immutable for the remaining test body;
    tests that mutate source or exercise drift detection must keep verifying every call.
    """
    import copy

    import review_prepare  # on sys.path once a fixture has prepared a run

    verified: dict[tuple[object, ...], tuple[dict[str, object], set[str]]] = {}
    real = review_prepare._source_snapshot

    def once(out, source_root, expected_head, paths, expected_diff_base, scope_path):
        key = (out, source_root, expected_head, tuple(paths), expected_diff_base, scope_path)
        if key not in verified:
            verified[key] = real(out, source_root, expected_head, paths, expected_diff_base, scope_path)
        return copy.deepcopy(verified[key])

    monkeypatch.setattr(review_prepare, "_source_snapshot", once)


def _batch_command(
    run: Path, operation: str, home: Path | None = None, *, source_only: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run installed commands, preserving absent-topology preparation for historical aggregate fixtures."""
    args = [sys.executable, str(preparation.HELPER), operation, "--out", str(run)]
    if operation == "prepare":
        root = json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
        args.extend(["--run-id", "bounded-review", "--parent-thread-id", "parent", "--source-root", root, "--batches"])
        if not source_only:
            script = """import runpy, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
import review_batches
original = review_batches.prepare_source_batches
def historical(out, plan, *args):
    \"\"\"Reproduce the historical frozen producer shape without a shipped legacy option.\"\"\"
    plan = dict(plan)
    plan.pop("review_topology", None)
    return original(out, plan, *args)
review_batches.prepare_source_batches = historical
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
            args = [sys.executable, "-c", script, *args[1:]]
    else:
        args.extend(["--codex-home", str(home)])
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=False)


def _completed_source_only(
    tmp_path: Path, *, retain_finding: bool = False, runtime_test: bool = False
) -> tuple[Path, Path]:
    """Admit every bounded source responsibility without creating report-review waves."""
    run = _batch_inputs(tmp_path)
    if runtime_test:
        source = Path(json.loads((run / "local-source/review-worktree.json").read_bytes())["source_worktree"])
        (source / "test_runtime.py").write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
            "def test_runtime():\n    import widget\n    assert widget.value == 2\n",
            encoding="utf-8",
            newline="\n",
        )
        original_run = run
        run = tmp_path / "runtime-review"
        run.mkdir()
        for path in original_run.iterdir():
            if path.is_file():
                (run / path.name).write_bytes(path.read_bytes())
        collected = subprocess.run(
            [
                sys.executable,
                str(preparation.PLUGIN_ROOT / "shared/collect_diff.py"),
                "--review-worktree",
                "--repository",
                str(source),
                "--out",
                str(run / "local-source"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert collected.returncode == 0, collected.stderr
        (run / "diff.patch").write_bytes((run / "local-source/diff.patch").read_bytes())
        (run / "untracked.txt").write_text("test_runtime.py\n", encoding="utf-8", newline="\n")
        briefs = json.loads((run / "review-briefs.json").read_bytes())
        for brief in briefs.values():
            brief["source_paths"].append("test_runtime.py")
        (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
    (run / "challenger-evidence.md").write_text("Bounded source review.\n" * 3500, encoding="utf-8", newline="\n")
    prepared = _batch_command(run, "prepare", source_only=True)
    assert prepared.returncode == 0, prepared.stderr
    inventory = json.loads((run / "batch-inventory.json").read_bytes())
    assert inventory["plan"]["review_topology"] == "source-only"
    home = tmp_path / "codex-home"
    for wave in json.loads((run / "batch-dispatch.json").read_bytes())["waves"]:
        directory = run / wave["directory"]
        roles = [
            entry["role_id"] for entry in json.loads((directory / "inspection-plan.json").read_bytes())["contexts"]
        ]
        preparation._assembly_evidence(
            tmp_path,
            prepared_run=directory,
            home=home,
            wave_index=wave["wave"],
            final_header="missing",
            findings={
                role: _batch_output([_batch_finding("F_EARLY", "low", "Document the returned value.")], 2)
                if retain_finding and wave["wave"] == 1 and role == "challenger"
                else _batch_output(
                    [],
                    1,
                    confidence={
                        "score": 0.90,
                        "scope": "Frozen source without execution.",
                        "gaps": [
                            {
                                "gap": "Runtime checks unavailable (-0.10)",
                                "status": "unresolved",
                                "rationale": "No execution evidence in source dispatch.",
                            }
                        ],
                    }
                    if runtime_test
                    else None,
                )
                for role in roles
            },
        )
        admitted = _batch_command(directory, "assemble-wave", home)
        assert admitted.returncode == 0, admitted.stderr
    return run, home


def test_source_only_review_reaches_ordinary_intake_without_report_review(tmp_path: Path) -> None:
    """Complete all source parts, preserving judgments at the next ordinary report consumer."""
    run, home = _completed_source_only(tmp_path)
    assembled = _batch_command(run, "assemble-batches", home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    assert manifest["review_topology"] == "source-only"
    assert all(item["source_parts"] for item in manifest["passes"])
    assert not (run / "interaction-dispatch.json").exists()
    assert not (run / "consolidation-dispatch.json").exists()
    for operation in ("prepare-interactions", "prepare-consolidation"):
        refused = _batch_command(run, operation, home)
        assert refused.returncode != 0
        assert "review-source-only-parent-reconciliation" in refused.stderr
    path = _canonical_report(run, [], {}, "accept-as-is")
    spec = importlib.util.spec_from_file_location(
        "source_only_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    assert finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent") == path


@pytest.mark.parametrize("source_only", [True, False])
@pytest.mark.parametrize("problem", ["missing-parts", "altered-part"])
def test_ordinary_intake_rejects_duplicate_role_metadata(tmp_path: Path, source_only: bool, problem: str) -> None:
    """An earlier duplicate cannot hide missing or forged source or historical final responsibility."""
    run, home = (
        _completed_source_only(tmp_path)
        if source_only
        else _completed_batches(tmp_path, large=True, consolidation_reports=3)
    )
    assert _batch_command(run, "assemble-batches", home).returncode == 0
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    passes = json.loads(json.dumps(manifest["passes"]))
    assert len({item["role"] for item in passes}) == len(passes) > 1
    duplicate = json.loads(json.dumps(passes[0]))
    parts_key = "source_parts" if source_only else "final_parts"
    if problem == "missing-parts":
        duplicate.pop(parts_key)
    else:
        duplicate[parts_key][0]["output_sha256"] = "0" * 64
        duplicate[parts_key][0]["confidence"]["score"] = 1.0
    path = _canonical_report(
        run, [], {}, "accept-as-is", confidence_metadata={"specialist_passes": [duplicate, *passes]}
    )
    spec = importlib.util.spec_from_file_location(
        "duplicate_role_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    with pytest.raises(LookupError, match=f"metadata-specialist-pass-duplicate-role:{duplicate['role']}"):
        finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")


def test_source_only_later_clean_part_cannot_erase_earlier_original(tmp_path: Path) -> None:
    """A later clean source part leaves the earlier finding actionable at ordinary remediation intake."""
    run, home = _completed_source_only(tmp_path, retain_finding=True)
    assert _batch_command(run, "assemble-batches", home).returncode == 0
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    (original,) = manifest["source_findings"]
    assert original["finding_id"] == "source-001.challenger.F_EARLY"
    challenger = next(item for item in manifest["passes"] if item["role"] == "challenger")
    assert len(challenger["source_parts"]) > 1
    assert challenger["source_parts"][0]["rating"] == 2
    assert challenger["source_parts"][-1]["rating"] == 1
    record = {
        **original["original"],
        "authors": ["Challenger"],
        "evidence": [original["manifest_path"], "widget.py:1-1"],
    }
    path = _canonical_report(run, [record], {original["finding_id"]: [record["id"]]}, "minor-changes")
    spec = importlib.util.spec_from_file_location(
        "earlier_source_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    assert finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent") == path
    assert finder.finding_evidence(path, record["id"])["source_origins"] == [original]
    path = _canonical_report(run, [], {}, "accept-as-is")
    with pytest.raises(LookupError, match="review-batch-source-findings-unresolved"):
        finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")


@pytest.mark.parametrize(
    "problem",
    [
        "missing-parts",
        "dropped-part",
        "output",
        "rating",
        "confidence",
        "attempt",
        "wave",
        "origin",
        "topology",
        "context-topology",
    ],
)
def test_source_only_intake_rejects_lost_or_altered_source_responsibility(tmp_path: Path, problem: str) -> None:
    """A final source assessment cannot hide an earlier source part or change frozen producer topology."""
    run, home = _completed_source_only(tmp_path)
    assert _batch_command(run, "assemble-batches", home).returncode == 0
    manifest_path = run / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    passes = json.loads(json.dumps(manifest["passes"]))
    item = next(item for item in passes if len(item["source_parts"]) > 1)
    extra = {"specialist_passes": passes}
    if problem == "missing-parts":
        item.pop("source_parts")
    elif problem == "dropped-part":
        item["source_parts"].pop(0)
    elif problem in {"output", "rating", "confidence", "attempt"}:
        part = item["source_parts"][0]
        if problem == "output":
            part["output_path"] = "specialists/unreviewed.md"
        elif problem == "rating":
            part["rating"] = 5
        elif problem == "confidence":
            part["confidence"]["score"] = 1.0
        else:
            part["attempt"]["context_sha256"] = "0" * 64
    elif problem == "wave":
        manifest["batch_execution"]["waves"].pop(0)
    elif problem == "origin":
        manifest["source_findings"] = [{"finding_id": "invented"}]
    elif problem == "topology":
        manifest.pop("review_topology")
    else:
        inventory_path = run / "batch-inventory.json"
        inventory = json.loads(inventory_path.read_bytes())
        inventory["plan"].pop("review_topology")
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
        manifest["batch_execution"]["inventory_sha256"] = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    path = _canonical_report(run, [], {}, "accept-as-is", confidence_metadata=extra)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "source_parts_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    with pytest.raises(LookupError, match="(metadata-specialist-pass-mismatch|review-batch)"):
        finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")


def test_historical_inventory_cannot_skip_report_coverage_with_an_aggregate_flag(tmp_path: Path) -> None:
    """Topology absent from the frozen producer requires every historical interaction obligation."""
    run, home = _completed_batches(tmp_path)
    assert _batch_command(run, "assemble-batches", home).returncode == 0
    path = run / "specialist-manifest.json"
    manifest = json.loads(path.read_bytes())
    assert "review_topology" not in json.loads((run / "batch-inventory.json").read_bytes())["plan"]
    manifest["review_topology"] = "source-only"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "historical_topology_validator", preparation.SKILL / "validate_artifacts.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    with pytest.raises(ValueError, match="review-batch-topology-mismatch"):
        validator._validate_review_runtime(run, manifest, manifest["passes"], home, "parent")


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


def _batch_output(
    records: list[dict[str, object]],
    rating: int,
    disposition: str | None = None,
    *,
    confidence: dict[str, object] | None = None,
) -> str:
    """Render the actual batch client response with explicit inventory and optional disposition."""
    profile = '{"score": 0.95, "scope": "Frozen source and declared interactions.", "gaps": [{"gap": "Synthetic offline client.", "status": "unresolved", "rationale": "Fixture proves admission without a live semantic reviewer (-0.05)."}]}'
    return (
        "## Reviewer Findings\n```json\n"
        + json.dumps(records)
        + "\n```\n\n"
        + ("## Finding Dispositions\n" + disposition + "\n\n" if disposition else "")
        + "## Reviewer Confidence\n```json\n"
        + (json.dumps(confidence) if confidence is not None else profile)
        + "\n```\n\n"
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
    fast_reviewers: bool = False,
    independent_required: bool = False,
    conditional_tail: bool = False,
    consolidation_reports: int = 0,
    consolidation_adverse: bool = False,
    consolidation_padding: int = 650,
    stop_before_consolidation: bool = False,
    consolidation_confidence: dict[str, object] | None = None,
    consolidation_prefix: bool = False,
) -> tuple[Path, Path]:
    """Record actual context-reader bytes for every source wave and final interaction wave."""
    run = (
        preparation._five_role_review_inputs(tmp_path)
        if five_roles
        else preparation._review_inputs(tmp_path)
        if single_file
        else _batch_inputs(tmp_path, large=large)
    )
    if independent_required:
        routing_path = run / "review-routing.json"
        routing = json.loads(routing_path.read_bytes())
        routing.update(
            independent_review_required=True,
            independence_requirement_evidence="The request requires independent source inspection.",
        )
        routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    if conditional_tail:
        routing_path = run / "review-routing.json"
        routing = json.loads(routing_path.read_bytes())
        routing["signals"].update(axis_doc_scribe=True, axis_web_explorer=True)
        routing["triggered_roles"] = sorted([*routing["triggered_roles"], "doc-scribe", "web-explorer"])
        briefs_path = run / "review-briefs.json"
        briefs = json.loads(briefs_path.read_bytes())
        for role in ("doc-scribe", "web-explorer"):
            routing["trigger_reasons"][role] = ["Declared conditional source inspection."]
            evidence = f"{role}-evidence.md"
            (run / evidence).write_text("Bounded conditional source review.\n" * 7000, encoding="utf-8", newline="\n")
            briefs[role] = {**briefs["qa-specialist"], "axis": role, "evidence_path": evidence}
        routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
        briefs_path.write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
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
            active_limit=1 if fast_reviewers else 4 if five_roles else None,
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
    padded_reports = 0
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
        if consolidation_reports:
            findings = findings or {}
            plan = json.loads((directory / "inspection-plan.json").read_bytes())
            for entry in plan["contexts"]:
                if padded_reports >= consolidation_reports:
                    break
                role = entry["role_id"]
                response = findings.get(role, _batch_output([], 1))
                findings[role] = response.replace(
                    "Frozen source and declared interactions.",
                    f"Report {padded_reports}: " + "Complete immutable reviewed claim. " * consolidation_padding,
                )
                padded_reports += 1
        preparation._assembly_evidence(
            tmp_path,
            prepared_run=directory,
            home=home,
            wave_index=index,
            final_header="missing",
            active_limit=1 if fast_reviewers else 4 if five_roles else None,
            findings=findings,
        )
        result = _batch_command(directory, "assemble-wave", home)
        assert result.returncode == 0, result.stderr
    if stop_before_consolidation:
        return run, home
    result = _batch_command(run, "prepare-consolidation", home)
    assert result.returncode == 0, result.stderr
    if consolidation_reports:
        final_schedule = json.loads((run / "consolidation-dispatch.json").read_bytes())
        for part, wave in enumerate(final_schedule["waves"]):
            directory = run / wave["directory"]
            findings = None
            if consolidation_confidence is not None and part == 0:
                findings = {"qa-specialist": _batch_output([], 1, confidence=consolidation_confidence)}
            if consolidation_adverse and part == 0:
                findings = {
                    "qa-specialist": _batch_output(
                        [_batch_finding("F_EARLY", "high", "Retain the early final defect.")], 5
                    )
                    .replace('"score": 0.95', '"score": 0.91')
                    .replace("(-0.05)", "(-0.09)")
                }
            if consolidation_prefix and part == 0:
                findings = findings or {}
                marker = preparation._CONTEXT_READER(
                    directory / "inspection-plan.json", "qa-specialist", 1, 1
                ).splitlines()[0]
                findings["qa-specialist"] = marker + "\n" + findings.get("qa-specialist", _batch_output([], 1))
            preparation._assembly_evidence(
                tmp_path,
                prepared_run=directory,
                home=home,
                wave_index=len(schedule["waves"]) + len(interaction_schedule["waves"]) + part + 1,
                final_header="missing",
                findings=findings,
            )
            result = _batch_command(directory, "assemble-wave", home)
            assert result.returncode == 0, result.stderr
        return run, home
    interactions = run / "batches/interactions"
    preparation._assembly_evidence(
        tmp_path,
        prepared_run=interactions,
        home=home,
        wave_index=len(schedule["waves"]) + len(interaction_schedule["waves"]) + 1,
        final_header="missing",
        active_limit=1 if fast_reviewers else 4 if five_roles else None,
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


def test_bounded_final_union_retains_early_adverse_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Admit complete bounded final comparisons without letting a clean last part erase an early defect."""
    run, home = _completed_batches(
        tmp_path, large=True, consolidation_reports=3, consolidation_adverse=True, consolidation_prefix=True
    )
    _verify_unchanged_source_once(monkeypatch)
    schedule = json.loads((run / "consolidation-dispatch.json").read_bytes())
    assert len(schedule["waves"]) > 1
    for wave in schedule["waves"]:
        plan = json.loads((run / wave["directory"] / "inspection-plan.json").read_bytes())
        for entry in plan["contexts"]:
            context = (run / wave["directory"] / entry["context_path"]).read_bytes()
            assert len(context) <= 65536
            assert (
                b"1 Approve, 2 Minor changes, 3 Changes required, 4 Insufficient evidence, 5 Block / Reject" in context
            )
            assert b"Deductions total exactly 1 minus score" in context
    assembled = _batch_command(run, "assemble-batches", home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    assert manifest["schema_version"] == 9
    qa = next(item for item in manifest["passes"] if item["role"] == "qa-specialist")
    assert len(qa["final_parts"]) == len(schedule["waves"])
    assert max(item["rating"] for item in qa["final_parts"]) == 5
    assert qa["final_parts"][-1]["rating"] == 1
    assert any(item["original"]["id"] == "F_EARLY" for item in manifest["source_findings"])
    assert qa["blocking_findings"] >= 1
    assert qa["confidence"] == 0.91
    assert qa["final_parts"][0]["confidence"]["gaps"][0]["status"] == "unresolved"
    first_output = run / qa["final_parts"][0]["output_path"]
    assert first_output.read_text(encoding="utf-8").startswith("<!-- codex-review-provenance role=qa-specialist ")
    import validate_artifacts

    by_role = validate_artifacts._validate_manifest_entries(
        run,
        manifest,
        manifest["passes"],
        {"challenger", "qa-specialist"},
        home,
        "parent",
        tmp_path,
        require_role_card_receipts=True,
    )
    assert by_role["qa-specialist"] == qa
    for field, changed, error in (
        ("confidence", 0.99, "review-batch-final-pass-mismatch"),
        ("blocking_findings", 0, "review-batch-final-pass-mismatch"),
        ("final_parts", qa["final_parts"][-1:], "review-batch-final-pass-mismatch"),
    ):
        tampered = json.loads(json.dumps(manifest))
        next(item for item in tampered["passes"] if item["role"] == "qa-specialist")[field] = changed
        with pytest.raises(ValueError, match=error):
            validate_artifacts._validate_manifest_entries(
                run, tampered, tampered["passes"], set(by_role), home, "parent", tmp_path
            )
    origin = next(item for item in manifest["source_findings"] if item["original"]["id"] == "F_EARLY")
    action = {
        **origin["original"],
        "authors": ["QA specialist"],
        "evidence": [origin["manifest_path"], "widget.py:1-1"],
    }
    path = _canonical_report(run, [action], {origin["finding_id"]: ["F_EARLY"]}, "needs-more-work")
    report = json.loads(path.read_bytes())
    evidence = validate_artifacts._validate_specialist_manifest(
        run,
        report,
        path,
        report["metadata"],
        "HIGH_RISK",
        validate_artifacts._ReviewEnvironment(home, "parent", tmp_path),
    )
    validate_artifacts._validate_reviewer_assessments(run, report["metadata"], evidence.by_role, batch_response=True)
    validate_artifacts._validate_independence_requirement(
        run,
        evidence,
        report["metadata"],
        "pass",
        "HIGH_RISK",
        validate_artifacts._ReviewEnvironment(home, "parent", tmp_path),
    )
    report["confidence"] = 0.95
    with pytest.raises(SystemExit, match="review-consolidation-confidence-inflated"):
        validate_artifacts._validate_specialist_manifest(
            run,
            report,
            path,
            report["metadata"],
            "HIGH_RISK",
            validate_artifacts._ReviewEnvironment(home, "parent", tmp_path),
        )
    report["confidence"] = 0.91
    report["metadata"]["confidence_gaps"] = []
    with pytest.raises(SystemExit, match="review-consolidation-confidence-gap-dropped"):
        validate_artifacts._validate_specialist_manifest(
            run,
            report,
            path,
            report["metadata"],
            "HIGH_RISK",
            validate_artifacts._ReviewEnvironment(home, "parent", tmp_path),
        )
    import review_batches

    schedule_path = run / "consolidation-dispatch.json"
    original_schedule = schedule_path.read_bytes()
    for mutation in ("tail", "pair", "role", "origin", "interval"):
        changed = json.loads(original_schedule)
        if mutation == "tail":
            changed["waves"].pop()
        elif mutation == "pair":
            changed["roles"]["qa-specialist"]["pairs"].pop()
        elif mutation == "role":
            changed["waves"][0]["roles"].remove("qa-specialist")
        elif mutation == "origin":
            changed["atoms"][0]["sha256"] = "0" * 64
        else:
            changed["roles"]["qa-specialist"]["groups"][0].append(0)
        schedule_path.write_bytes(json.dumps(changed).encode())
        candidate = json.loads(json.dumps(manifest))
        candidate["batch_execution"]["consolidation_sha256"] = review_batches._digest(schedule_path.read_bytes())
        with pytest.raises(ValueError, match="review-consolidation-schedule-mismatch"):
            review_batches.validate_aggregate(run, candidate, set(by_role), home, "parent")
    schedule_path.write_bytes(original_schedule)
    candidate = json.loads(json.dumps(manifest))
    candidate["batch_execution"]["waves"].pop()
    with pytest.raises(ValueError, match="review-batch-wave-coverage"):
        review_batches.validate_aggregate(run, candidate, set(by_role), home, "parent")
    candidate = json.loads(json.dumps(manifest))
    candidate["source_findings"] = []
    with pytest.raises(ValueError, match="review-batch-source-finding-disposition-mismatch"):
        review_batches.validate_aggregate(run, candidate, set(by_role), home, "parent")
    context_path = run / schedule["waves"][0]["directory"] / "specialists/qa-specialist-context.md"
    frozen_context = context_path.read_bytes()
    context_path.write_bytes(frozen_context + b"\nUnproved extra payload.\n")
    with pytest.raises(SystemExit, match="(provenance-context-hash-mismatch|review-inspection-contexts-invalid)"):
        review_batches.validate_aggregate(run, manifest, set(by_role), home, "parent")
    context_path.write_bytes(frozen_context)
    native = json.loads((run / schedule["waves"][0]["directory"] / "specialist-manifest.json").read_bytes())
    native["schema_version"] = 9
    with pytest.raises(SystemExit, match="manifest-schema-nine-aggregate-only"):
        validate_artifacts._validate_manifest_entries(
            run, native, native["passes"], set(by_role), home, "parent", tmp_path
        )


def test_final_union_rejects_oversized_indivisible_report(tmp_path: Path) -> None:
    """Reject a proved report too large for paired semantic delivery before creating any final dispatch."""
    run, home = _completed_batches(
        tmp_path, large=True, consolidation_reports=3, consolidation_padding=1000, stop_before_consolidation=True
    )
    prepared = _batch_command(run, "prepare-consolidation", home)
    assert prepared.returncode == 2
    assert "review-consolidation-atomic-capacity:" in prepared.stderr
    assert not (run / "consolidation-dispatch.json").exists()
    assert not list((run / "batches").glob("consolidation-*"))


def test_new_batch_generation_supplies_rating_legend_to_every_final_and_interaction_part(tmp_path: Path) -> None:
    """Keep fresh bounded reviewers' rating scale explicit without changing native manifest generations."""
    run, home = _completed_batches(tmp_path)
    assert json.loads((run / "batch-inventory.json").read_bytes())["schema_version"] == 2
    directories = [*sorted((run / "batches").glob("interaction-*")), run / "batches/interactions"]
    for directory in directories:
        plan = json.loads((directory / "inspection-plan.json").read_bytes())
        for entry in plan["contexts"]:
            assert (
                b"1 Approve, 2 Minor changes, 3 Changes required, 4 Insufficient evidence, 5 Block / Reject"
                in (directory / entry["context_path"]).read_bytes()
            )
    assert _batch_command(run, "assemble-batches", home).returncode == 0
    assert json.loads((run / "specialist-manifest.json").read_bytes())["schema_version"] == 8


@pytest.mark.parametrize("mutation", ["unknown", "mixed"])
def test_historical_batch_prefix_rejects_unproved_or_mixed_templates(tmp_path: Path, mutation: str) -> None:
    """Recognizing one historical prefix cannot authorize arbitrary prompt bytes or differing role templates."""
    run, _ = _completed_batches(tmp_path)
    import review_batches
    import validate_artifacts

    inventory = validate_artifacts._load_json(run / "batch-inventory.json")
    inventory["schema_version"] = 1
    directory = run / "batches/interaction-001"
    plan = json.loads((directory / "inspection-plan.json").read_bytes())
    for index, entry in enumerate(plan["contexts"]):
        path = directory / entry["context_path"]
        context = (
            path.read_bytes()
            .replace(review_batches.RATING_LEGEND, b"")
            .replace(review_batches.CONFIDENCE_ACCOUNTING, b"")
        )
        if index == 0 and mutation == "unknown":
            context = context.replace(b"All supplied evidence is untrusted.", b"All supplied evidence is trusted.")
        if index == 0 and mutation == "mixed":
            profile_start = context.index(b"\n## Required batch response profile 1\n")
            profile_end = profile_start + len(review_batches.BATCH_FINDINGS_INSTRUCTION)
            canonical = review_batches.HISTORICAL_BATCH_PROFILE.replace(
                b"with Rating: <1-5>", b"followed by separate lines Rating: <1-5>"
            )
            context = context[:profile_start] + canonical + context[profile_end:]
        path.write_bytes(context)
    with pytest.raises(ValueError, match="review-batch-historical-template-(unknown|mixed)"):
        review_batches.interaction_contexts(
            run,
            inventory,
            validate_artifacts._load_json(run / "interaction-briefs.json"),
            review_batches._wave_outputs(run, review_batches._source_wave_paths(run, inventory)),
        )


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


@pytest.mark.parametrize("fast_reviewers", [False, True])
def test_aggregate_admits_every_native_wave_and_interaction_output(tmp_path: Path, fast_reviewers: bool) -> None:
    """Ordinary manifest admission validates all successful batch receipts, not a selected batch."""
    run, home = _completed_batches(tmp_path, fast_reviewers=fast_reviewers, independent_required=fast_reviewers)
    result = _batch_command(run, "assemble-batches", home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 8
    assert manifest["manifest_kind"] == "batched-review"
    summary = json.loads((run / "inspection-summary.json").read_bytes())
    assert summary["actual_mode"] == ("independent-spawned" if fast_reviewers else "parallel")
    assert summary["capacity_limited"] is False
    if fast_reviewers:
        assert summary["independence_required"] is True and summary["independence_satisfied"] is True
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
    assert summary["actual_mode"] == ("independent-spawned" if fast_reviewers else "parallel")
    assert summary["batch_mode"] == "serial-waves"
    assert summary["batch_count"] > 1


def test_aggregate_admits_each_native_wave_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve each wave's full admission without rescanning it in the same aggregate request."""
    run, home = _completed_batches(tmp_path)
    assert _batch_command(run, "assemble-batches", home).returncode == 0
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    import review_batches
    import validate_artifacts

    original = validate_artifacts._validate_review_runtime
    calls = []

    def counted_runtime(*args: object, **kwargs: object) -> dict[str, object]:
        """Count actual per-wave checks while keeping receipt validation active."""
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(validate_artifacts, "_validate_review_runtime", counted_runtime)
    summary = review_batches.validate_aggregate(run, manifest, {"challenger", "qa-specialist"}, home, "parent")
    assert summary == json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    assert len(calls) == len(manifest["batch_execution"]["waves"])


def test_ordinary_consumers_admit_batched_runtime_once_each(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep full native admission while avoiding a second aggregate scan in each consumer."""
    run, home = _completed_batches(tmp_path)
    assert _batch_command(run, "assemble-batches", home).returncode == 0
    result_path = _canonical_report(run, [], {}, "accept-as-is")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location(
        "single_aggregate_validator", preparation.SKILL / "validate_artifacts.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    import review_batches

    original = review_batches.validate_aggregate
    calls = []

    def counted_aggregate(*args: object, **kwargs: object) -> dict[str, object]:
        """Count complete admission calls while retaining native receipt and source checks."""
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(review_batches, "validate_aggregate", counted_aggregate)
    validator._validate_manifest_preflight(run, home, "parent", tmp_path)
    assert len(calls) == 1
    evidence = validator._validate_specialist_manifest(
        run,
        result,
        result_path,
        result["metadata"],
        "HIGH_RISK",
        validator._ReviewEnvironment(home, "parent", tmp_path),
    )
    assert len(calls) == 2
    assert evidence.runtime_summary == json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    assert (
        validator._validate_review_runtime(run, evidence.manifest, evidence.passes, home, "parent")
        == evidence.runtime_summary
    )
    assert len(calls) == 3


def test_ordinary_consumers_admit_native_runtime_once_each(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain native wave evidence checks without repeating them inside one admission."""
    run, home, _ = preparation._assembly_evidence(tmp_path)
    assert preparation._assemble(run, home).returncode == 0
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    metadata = {
        "specialist_manifest": "specialist-manifest.json",
        "execution_mode": summary["actual_mode"],
        "execution_evidence_level": summary["evidence_level"],
        "write_parallel_eligible": False,
        "execution_observed_controls": summary["observed_controls"],
        "review_run_id": manifest["review_run_id"],
        "review_input_sha256": manifest["review_input_sha256"],
    }
    spec = importlib.util.spec_from_file_location(
        "single_native_validator", preparation.SKILL / "validate_artifacts.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    original = validator._validate_review_runtime
    calls = []

    def counted_runtime(*args: object, **kwargs: object) -> dict[str, object]:
        """Count real runtime validation in both ordinary admission routes."""
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(validator, "_validate_review_runtime", counted_runtime)
    validator._validate_manifest_preflight(run, home, "parent", tmp_path)
    assert len(calls) == 1
    evidence = validator._validate_specialist_manifest(
        run,
        {"schema_version": 3},
        run / "result-candidate.json",
        metadata,
        "HIGH_RISK",
        validator._ReviewEnvironment(home, "parent", tmp_path),
    )
    assert len(calls) == 2
    assert evidence.runtime_summary == summary
    assert validator._validate_review_runtime(run, manifest, manifest["passes"], home, "parent") == summary
    assert len(calls) == 3


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
        validator._validate_batch_source_findings(
            run, {"status": "pass", "metadata": metadata, "findings": {}}, manifest
        )
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
        run, {"status": "fail", "metadata": metadata, "findings": {"high": 1}}, manifest
    )
    metadata["source_finding_mapping"][unresolved[0]["finding_id"]] = []
    with pytest.raises(SystemExit, match="review-batch-result-source-findings-dropped"):
        validator._validate_batch_source_findings(
            run, {"status": "fail", "metadata": metadata, "findings": {}}, manifest
        )


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


@pytest.mark.integration
def test_batch_assembly_accepts_documented_assessment_shape_and_retains_source_axis(tmp_path: Path) -> None:
    """Derive immutable routing axes without adding undocumented fields to reviewer assessments."""
    run = _batch_inputs(tmp_path)
    briefs_path = run / "review-briefs.json"
    briefs = json.loads(briefs_path.read_bytes())
    for role, brief in briefs.items():
        brief["axis"] = f"Frozen contract boundary for {role}"
    briefs_path.write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    prepared = _batch_command(run, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    wave = run / "batches/source-001"
    home = tmp_path / "codex-home"
    _, _, children = preparation._assembly_evidence(
        tmp_path,
        prepared_run=wave,
        home=home,
        wave_index=1,
        final_header="missing",
        findings={
            "challenger": _batch_output([], 4).replace('"score": 0.95', '"score": 0.78'),
            "qa-specialist": _batch_output([], 1),
        },
    )
    assessments_path = wave / "specialist-assessments.json"
    assessments = json.loads(assessments_path.read_bytes())
    assessments["challenger"]["confidence"] = 0.78
    assessments_path.write_text(json.dumps(assessments), encoding="utf-8", newline="\n")
    assessments_bytes = assessments_path.read_bytes()
    assessments = json.loads(assessments_bytes)
    assert all(set(assessment) == {"confidence", "blocking_findings"} for assessment in assessments.values())
    child_bytes = {role: path.read_bytes() for role, path in children.items()}
    assembled = _batch_command(wave, "assemble-wave", home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((wave / "specialist-manifest.json").read_bytes())
    briefs = json.loads((run / "review-briefs.json").read_bytes())
    for item in manifest["passes"]:
        role = item["role"]
        assert item["axis"] == briefs[role]["axis"]
        assert item["confidence"] == assessments[role]["confidence"]
        assert item["blocking_findings"] == assessments[role]["blocking_findings"]
        terminal = json.loads(child_bytes[role].splitlines()[-1])["payload"]["last_agent_message"]
        assert (wave / item["attempts"][0]["raw_output_path"]).read_bytes() == terminal.encode("utf-8")
    assert assessments_path.read_bytes() == assessments_bytes
    assert {role: path.read_bytes() for role, path in children.items()} == child_bytes


def test_batch_wave_producer_admits_runtime_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Count complete runtime admission in the native batch wave producer."""
    run = _batch_inputs(tmp_path)
    assert _batch_command(run, "prepare").returncode == 0
    directory = run / "batches/source-001"
    home = tmp_path / "codex-home"
    preparation._assembly_evidence(tmp_path, prepared_run=directory, home=home, wave_index=1, final_header="missing")
    import review_batches
    import validate_artifacts

    original = validate_artifacts._validate_review_runtime
    calls = []

    def counted_runtime(*args: object, **kwargs: object) -> dict[str, object]:
        """Count native validation while preserving the real checks."""
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(validate_artifacts, "_validate_review_runtime", counted_runtime)
    summary = review_batches.assemble_wave(directory, home)
    assert summary == json.loads((directory / "inspection-summary.json").read_text(encoding="utf-8"))
    assert len(calls) == 1


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
        validator._validate_batch_source_findings(run, result, manifest)
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
    validator._validate_batch_source_findings(run, result, manifest)


def test_mixed_source_findings_keep_each_original_severity(tmp_path: Path) -> None:
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
        tmp_path,
        {"status": "fail", "metadata": metadata},
        {"manifest_kind": "batched-review", "source_findings": ledger},
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
    confidence_metadata: dict[str, object] | None = None,
    retained_gates: dict[str, object] | None = None,
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
    confidence = 0.95
    if manifest["schema_version"] == 9:
        confidence = min(item["confidence"] for item in manifest["passes"])
        for item in manifest["passes"]:
            for part in item["source_parts" if manifest.get("review_topology") == "source-only" else "final_parts"]:
                for gap in part["confidence"]["gaps"]:
                    if gap["status"] != "closed":
                        label = (
                            f"{item['role']} {part['output_path']}: {gap['gap']} ({gap['status']}) - {gap['rationale']}"
                        )
                        metadata["confidence_gaps"].append(label)
                        metadata["confidence_gap_closures"].append(
                            {"gap": label, "status": gap["status"], "rationale": gap["rationale"]}
                        )
        metadata["confidence_recovery"]["initial_confidence"] = confidence
        metadata["confidence_recovery"]["final_confidence"] = confidence
    if confidence_metadata is not None:
        metadata.update(confidence_metadata)
        confidence = metadata["confidence_recovery"]["final_confidence"]
    result = {
        "schema_version": 3,
        "status": status,
        "confidence": confidence,
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
    if retained_gates is None:
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
    else:
        checks = retained_gates["checks"]
        (run / "gates.json").write_text(json.dumps(retained_gates), encoding="utf-8", newline="\n")
    fields = [
        ("Scope", "working-tree"),
        ("Revision", "diff sha256:" + metadata["review_input_sha256"]),
        ("CI", "unavailable"),
        ("Type", "fix"),
        (
            "Suggestion",
            {
                "accept-as-is": "approve",
                "minor-changes": "minor changes",
                "needs-more-work": "needs work",
                "reject": "reject",
                "not-aligned": "not aligned",
            }[recommendation],
        ),
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
            "score": confidence,
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


@pytest.mark.parametrize(
    "problem",
    [
        "none",
        "origin",
        "author",
        "pointer",
        "coordinate",
        "original",
        "severity",
        "digest",
        "range",
        "witness",
        "reason",
        "duplicates",
        "unknown-group",
        "inventory",
        "no-reconciliation",
        "identical-origins",
    ],
)
def test_semantic_group_preserves_origins_at_ordinary_report_intake(tmp_path: Path, problem: str) -> None:
    """Wording variants share one evidenced action while every original obligation reaches remediation."""
    first = _batch_finding("F_ONE", "medium", "Make the result distinguish an input transpose.")
    duplicate = {**first, "id": "F_COPY"}
    variant = _batch_finding("F_VARIANT", "low", "Use an asymmetric input to detect a transposed result.")
    variant["closure_evidence"] = "The correct orientation passes and a deliberately transposed result fails."
    variant["evidence"] = [{"path": "other.py", "start_line": 1, "end_line": 1}]
    if problem == "identical-origins":
        variant = {**first, "id": "F_VARIANT"}
    run, home = _completed_batches(
        tmp_path,
        blocker=(1, "challenger"),
        source_records=[first, duplicate],
        interaction_finding=(1, "qa-specialist", 3),
        interaction_records=[variant],
    )
    assert _batch_command(run, "assemble-batches", home).returncode == 0
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    originals = manifest["source_findings"]
    assert len(originals) == 3
    record = {
        **first,
        "id": "F_CANONICAL",
        "authors": ["Challenger", "QA specialist"],
        "summary": "The oracle must detect a transposed result.",
        "required_change": "Use asymmetric input to make the result distinguish an input transpose.",
        "closure_evidence": variant["closure_evidence"],
        "evidence": [*[item["manifest_path"] for item in originals], "widget.py:1-1", "other.py:1-1"],
    }
    exact = sorted(item["finding_id"] for item in originals if item["original"]["id"] != "F_VARIANT")
    duplicates = {exact[1]: exact[0]}
    if problem == "identical-origins":
        exact = sorted(item["finding_id"] for item in originals)
        duplicates = {identity: exact[0] for identity in exact[1:]}
    group = {
        "invariant": "The test oracle distinguishes orientation.",
        "rationale": "All originals require the same asymmetric oracle and transpose negative control.",
        "origins": {
            item[
                "finding_id"
            ]: "The asymmetric input and transpose control cover this original change and closure demand."
            for item in originals
        },
        "evidence": [originals[0]["original"]["evidence"][0].copy()],
    }
    reconciliation = {"F_CANONICAL": group}
    extra = {"source_finding_reconciliation": reconciliation}
    if problem == "origin":
        group["origins"].pop(originals[0]["finding_id"])
    elif problem == "author":
        record["authors"] = ["Challenger"]
    elif problem == "pointer":
        record["evidence"] = [entry for entry in record["evidence"] if entry != originals[0]["manifest_path"]]
    elif problem == "coordinate":
        record["evidence"].remove("other.py:1-1")
    elif problem == "original":
        extra["source_findings"] = json.loads(json.dumps(originals))
        extra["source_findings"][0]["original"]["required_change"] = "Discard the original obligation."
    elif problem == "severity":
        record["severity"] = "low"
    elif problem == "digest":
        group["evidence"][0]["source_sha256"] = "0" * 64
    elif problem == "range":
        group["evidence"][0]["end_line"] = 999999
    elif problem == "witness":
        group["evidence"] = []
    elif problem == "reason":
        group["origins"][originals[0]["finding_id"]] = " "
    elif problem == "duplicates":
        duplicates[originals[-1]["finding_id"]] = exact[0]
    elif problem == "unknown-group":
        reconciliation["UNKNOWN"] = group
    elif problem == "inventory":
        manifest["batch_execution"]["inventory_sha256"] = "0" * 64
        (run / "specialist-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    elif problem == "no-reconciliation":
        extra = {}
    path = _canonical_report(
        run,
        [record],
        {item["finding_id"]: ["F_CANONICAL"] for item in originals},
        "needs-more-work",
        status="fail",
        duplicates=duplicates,
        confidence_metadata=extra,
    )
    spec = importlib.util.spec_from_file_location(
        "semantic_consumer_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    if problem != "none":
        with pytest.raises(LookupError, match="review-batch"):
            finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")
        return
    assert finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent") == path
    evidence = finder.finding_evidence(path, "F_CANONICAL")
    assert evidence["source_origins"] == originals
    assert evidence["source_reconciliation"] == group
    assert json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))["source_findings"] == originals


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


@pytest.mark.parametrize("route", ["interaction", "fitting-final"])
def test_unissued_historical_inventory_uses_complete_current_instructions(tmp_path: Path, route: str) -> None:
    """Fresh work on a retained generation must explain ratings and confidence without changing issued templates."""
    run, _ = _completed_batches(tmp_path)
    import review_batches
    import validate_artifacts

    inventory = validate_artifacts._load_json(run / "batch-inventory.json")
    inventory["schema_version"] = 1
    directory = run / "batches" / ("interaction-001" if route == "interaction" else "interactions")
    plan = validate_artifacts._load_json(directory / "inspection-plan.json")
    for entry in plan["contexts"]:
        (directory / entry["context_path"]).unlink()
    if route == "interaction":
        rendered = review_batches.interaction_contexts(
            run,
            inventory,
            validate_artifacts._load_json(run / "interaction-briefs.json"),
            review_batches._wave_outputs(run, review_batches._source_wave_paths(run, inventory)),
        )
        contexts = {role: packets[0] for role, packets in rendered.items()}
    else:
        contexts = review_batches.consolidation_contexts(run, inventory)
    for role, content in contexts.items():
        assert review_batches.RATING_LEGEND in content
        assert review_batches.CONFIDENCE_ACCOUNTING in content
        # The newly issued full prefix must itself remain a recognized exact historical template.
        (directory / "specialists" / f"{role}-context.md").write_bytes(content)
    if route == "interaction":
        repeated = review_batches.interaction_contexts(
            run,
            inventory,
            validate_artifacts._load_json(run / "interaction-briefs.json"),
            review_batches._wave_outputs(run, review_batches._source_wave_paths(run, inventory)),
        )
        assert {role: packets[0] for role, packets in repeated.items()} == contexts
    else:
        assert review_batches.consolidation_contexts(run, inventory) == contexts


def test_multipart_confidence_recovery_retains_scoped_judgments(tmp_path: Path) -> None:
    """Allow fully mapped global recovery without altering historical scores or accepting invented evidence."""
    run, home = _completed_batches(tmp_path, large=True, consolidation_reports=3)
    import validate_artifacts

    assembled = _batch_command(run, "assemble-batches", home)
    assert assembled.returncode == 0, assembled.stderr
    manifest_bytes = (run / "specialist-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    first_wave = run / manifest["batch_execution"]["waves"][0]["manifest_path"]
    selected = json.loads(first_wave.read_bytes())["passes"][0]
    evidence_path = (first_wave.parent / selected["output_path"]).relative_to(run).as_posix()
    labels = [
        f"{item['role']} {part['output_path']}: {gap['gap']} ({gap['status']}) - {gap['rationale']}"
        for item in manifest["passes"]
        for part in item["final_parts"]
        for gap in part["confidence"]["gaps"]
        if gap["status"] != "closed"
    ]
    residual = "Residual: controlled offline receipt uncertainty."
    confidence_metadata = {
        "confidence_gaps": [*labels, residual],
        "confidence_gap_closures": [
            {
                "gap": label,
                "status": "closed",
                "evidence": evidence_path,
                "rationale": "(-0.00) Complete admitted source inspection resolves this scoped fixture limitation.",
            }
            for label in labels
        ]
        + [{"gap": residual, "status": "unresolved", "rationale": "(-0.01) Offline receipts remain a limit."}],
        "confidence_recovery": {
            "initial_confidence": 0.95,
            "final_confidence": 0.99,
            "status": "fair",
            "evidence": [evidence_path],
            "recovery_actions": ["Reconcile all scoped causes against admitted source inspection."],
            "remaining_limits": [f"(-0.00) Same cause as {residual}"],
        },
    }
    path = _canonical_report(run, [], {}, "accept-as-is", confidence_metadata=confidence_metadata)
    validate_artifacts._validate_result(run, path, home, "parent", tmp_path)
    assert (run / "specialist-manifest.json").read_bytes() == manifest_bytes
    assert {part["confidence"]["score"] for item in manifest["passes"] for part in item["final_parts"]} == {0.95}
    spec = importlib.util.spec_from_file_location(
        "confidence_recovery_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    assert finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent") == path
    shared = json.loads(json.dumps(confidence_metadata))
    for closure in shared["confidence_gap_closures"][:-1]:
        closure["status"] = "unresolved"
        closure["rationale"] = f"(-0.00) Same cause as {residual} Count the offline limitation once globally."
    path = _canonical_report(run, [], {}, "accept-as-is", confidence_metadata=shared)
    validate_artifacts._validate_result(run, path, home, "parent", tmp_path)
    # Quoted immutable labels themselves contain historical deductions; only the leading current token counts.
    counted = labels[0]
    shared["confidence_gap_closures"][0]["rationale"] = "(-0.01) Count the remaining offline uncertainty once."
    for closure in shared["confidence_gap_closures"][1:]:
        closure["rationale"] = f"(-0.00) Same cause as {counted}"
    path = _canonical_report(run, [], {}, "accept-as-is", confidence_metadata=shared)
    validate_artifacts._validate_result(run, path, home, "parent", tmp_path)
    for mutation, expected in (
        ("dropped", "review-consolidation-confidence-gap-dropped"),
        ("missing-evidence", "review-consolidation-confidence-evidence-invalid"),
        ("unadmitted-evidence", "review-consolidation-confidence-evidence-invalid"),
        ("closed-deduction", "review-consolidation-confidence-accounting-invalid"),
        ("wrong-total", "review-consolidation-confidence-accounting-invalid"),
        ("wrong-initial", "review-consolidation-confidence-initial-mismatch"),
        ("conflicting-evidence", "review-consolidation-confidence-evidence-invalid"),
        ("unknown-scoped-gap", "review-consolidation-confidence-gap-unknown"),
        ("unproved-reduction", "review-consolidation-confidence-evidence-invalid"),
    ):
        changed = json.loads(json.dumps(confidence_metadata))
        closure = changed["confidence_gap_closures"][0]
        if mutation == "dropped":
            changed["confidence_gaps"].remove(labels[0])
            changed["confidence_gap_closures"].pop(0)
        elif mutation == "missing-evidence":
            closure["evidence"] = "Claimed closed without a receipt."
        elif mutation == "unadmitted-evidence":
            closure["evidence"] = "review-notes.md"
        elif mutation == "closed-deduction":
            closure["rationale"] = "(-0.01) Closed but still deducted."
        elif mutation == "wrong-total":
            changed["confidence_gap_closures"][-1]["rationale"] = "(-0.02) Wrong total."
        elif mutation == "conflicting-evidence":
            closure["evidence_path"] = "review-notes.md"
        elif mutation == "unknown-scoped-gap":
            unknown = labels[0].split(": ", 1)[0] + ": Invented scoped gap."
            changed["confidence_gaps"].append(unknown)
            changed["confidence_gap_closures"].append({**closure, "gap": unknown})
        elif mutation == "unproved-reduction":
            closure["status"] = "unresolved"
            closure.pop("evidence")
            closure["rationale"] = "(-0.00) Lowered without evidence or a shared cause."
        else:
            changed["confidence_recovery"]["initial_confidence"] = 0.96
        path = _canonical_report(run, [], {}, "accept-as-is", confidence_metadata=changed)
        with pytest.raises(SystemExit, match=expected):
            validate_artifacts._validate_result(run, path, home, "parent", tmp_path)


@pytest.mark.parametrize("placement", ["gap", "rationale", "duplicate"])
def test_emitted_scoped_deductions_require_evidence_for_reduction(tmp_path: Path, placement: str) -> None:
    """Bind permitted raw token positions through native assembly before checking a global reduction."""
    profile = {"score": 0.70, "scope": "Bounded implementation and regression inspection.", "gaps": []}
    for name, amount in [
        ("Production source and exact diff unavailable", "25"),
        ("Mutation evidence unavailable", "05"),
    ]:
        token = f"(-0.{amount})"
        profile["gaps"].append(
            {
                "gap": name + (f" {token}" if placement in {"gap", "duplicate"} else ""),
                "status": "unresolved",
                "rationale": "Full global evidence has not yet been inspected."
                + (f" {token}" if placement in {"rationale", "duplicate"} else ""),
            }
        )
    run, home = _completed_batches(tmp_path, large=True, consolidation_reports=3, consolidation_confidence=profile)
    import validate_artifacts

    assert _batch_command(run, "assemble-batches", home).returncode == 0
    manifest_bytes = (run / "specialist-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    part = next(item for item in manifest["passes"] if item["role"] == "qa-specialist")["final_parts"][0]
    assert part["confidence"] == profile
    raw_bytes = (run / part["output_path"]).read_bytes()
    labels = []
    closures = []
    for item in manifest["passes"]:
        for current in item["final_parts"]:
            for index, gap in enumerate(current["confidence"]["gaps"]):
                if gap["status"] == "closed":
                    continue
                label = f"{item['role']} {current['output_path']}: {gap['gap']} ({gap['status']}) - {gap['rationale']}"
                labels.append(label)
                closures.append(
                    {"gap": label, "status": "unresolved", "rationale": "(-0.00) Same cause as pending global cause."}
                )
                if current == part:
                    closures[-1]["rationale"] = (
                        "(-0.01) Production still absent; no cause evidence."
                        if index == 0
                        else "(-0.05) Mutation remains absent."
                    )
    production = next(closure["gap"] for closure in closures if closure["rationale"].startswith("(-0.01)"))
    for closure in closures:
        if closure["rationale"].startswith("(-0.00)"):
            closure["rationale"] = f"(-0.00) Same cause as {production}"
    evidence = manifest["passes"][0]["final_parts"][-1]["output_path"]
    metadata = {
        "confidence_gaps": labels,
        "confidence_gap_closures": closures,
        "confidence_recovery": {
            "initial_confidence": 0.70,
            "final_confidence": 0.94,
            "status": "fair",
            "evidence": [evidence],
            "recovery_actions": ["Inventory global scoped limitations."],
            "remaining_limits": [f"(-0.00) Same cause as {production}"],
        },
    }
    path = _canonical_report(run, [], {}, "accept-as-is", confidence_metadata=metadata)
    with pytest.raises(SystemExit, match="review-consolidation-confidence-evidence-invalid"):
        validate_artifacts._validate_result(run, path, home, "parent", tmp_path)
    spec = importlib.util.spec_from_file_location(
        "scoped_token_intake", preparation.SKILL.parents[1] / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = finder
    spec.loader.exec_module(finder)
    with pytest.raises(LookupError, match="review-consolidation-confidence-evidence-invalid"):
        finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")
    # The counted cause gets its own admitted reference; other occurrences remain explicit shared-cause mappings.
    next(closure for closure in closures if closure["gap"] == production)["evidence"] = evidence
    path = _canonical_report(run, [], {}, "accept-as-is", confidence_metadata=metadata)
    validate_artifacts._validate_result(run, path, home, "parent", tmp_path)
    assert finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent") == path
    assert (run / part["output_path"]).read_bytes() == raw_bytes
    assert (run / "specialist-manifest.json").read_bytes() == manifest_bytes


@pytest.mark.parametrize(
    "gap,rationale,expected",
    [
        pytest.param("Missing evidence (-0.25).", "Evidence is absent.", 25, id="gap-only"),
        pytest.param("Missing evidence.", "Evidence is absent (-0.25).", 25, id="rationale-only"),
        pytest.param("Missing evidence (-0.25).", "Evidence is absent (-0.25).", 25, id="duplicate-identical"),
        pytest.param("Missing evidence (-1.00).", "Evidence is absent.", 100, id="historical-full-shortfall"),
        pytest.param("Missing evidence (−0.25).", "Evidence is absent.", 25, id="historical-unicode-minus"),
        pytest.param("Missing evidence.", "No numerical attribution supplied.", 0, id="explicit-residual-needed"),
        pytest.param("Missing evidence (-0.25).", "Evidence is absent (-0.05).", None, id="conflicting-fields"),
        pytest.param(
            "Missing evidence (-0.25) and (-0.05).", "Ambiguous cause attribution.", None, id="ambiguous-own-field"
        ),
    ],
)
def test_scoped_contribution_positions_are_unambiguous(gap: str, rationale: str, expected: int | None) -> None:
    """Preserve historical token spellings while refusing conflicting attribution instead of dropping its cost."""
    helper = preparation.runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))[
        "_scoped_confidence_deduction"
    ]
    profile = {"gap": gap, "rationale": rationale}
    if expected is None:
        with pytest.raises(SystemExit, match="review-consolidation-confidence-scoped-deduction-ambiguous"):
            helper(profile)
    else:
        assert helper(profile) == expected


@pytest.mark.parametrize("workers", [0, 2])
def test_local_source_gate_evidence_reaches_ordinary_confidence_intake(tmp_path: Path, workers: int) -> None:
    """Current import-bound local tests can close runtime gaps without changing source scores."""
    run, home = _completed_source_only(tmp_path, runtime_test=True)
    assert _batch_command(run, "assemble-batches", home).returncode == 0
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    mirror = json.loads((run / "local-source/review-worktree.json").read_bytes())["review_worktree"]
    args = [
        sys.executable,
        str(preparation.PLUGIN_ROOT / "shared/run_gates.py"),
        "--out",
        str(run),
        "--worktree",
        mirror,
        "--pytest-python",
        sys.executable,
        "--pytest-import",
        "widget",
        "--pytest-args-json",
        json.dumps(
            [
                "-q",
                *(["-n", str(workers)] if workers else ["-p", "no:xdist"]),
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                "test_runtime.py",
            ]
        ),
    ]
    for gate in ("lint", "format", "types", "review"):
        args.extend((f"--{gate}", f"{sys.executable} -c pass"))
    completed = subprocess.run(
        args, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    gates_bytes = (run / "gates.json").read_bytes()
    actual_proof = next(check for check in json.loads(gates_bytes)["checks"] if check["id"] == "tests")[
        "python_imports"
    ]
    if workers:
        assert len(actual_proof["workers"]) == workers
        assert any(worker["status"] == "inconclusive" for worker in actual_proof["workers"].values())
        assert actual_proof["status"] == "pass"
    spec = importlib.util.spec_from_file_location("local_gate_consumer", preparation.SKILL / "validate_artifacts.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    assert "checks/tests.stdout.txt" in validator._consolidation_confidence_evidence(run, manifest)
    labels = [
        f"{item['role']} {part['output_path']}: {gap['gap']} ({gap['status']}) - {gap['rationale']}"
        for item in manifest["passes"]
        for part in item["source_parts"]
        for gap in part["confidence"]["gaps"]
    ]
    metadata = {
        "confidence_gaps": ["Synthetic offline receipts.", *labels],
        "confidence_gap_closures": [
            {
                "gap": "Synthetic offline receipts.",
                "status": "unresolved",
                "rationale": "(-0.05) Native specialist receipts are synthetic.",
            },
            *[
                {
                    "gap": label,
                    "status": "closed",
                    "rationale": "(-0.00) Current mirror tests executed.",
                    "evidence": "checks/tests.stdout.txt",
                }
                for label in labels
            ],
        ],
        "confidence_recovery": {
            "initial_confidence": 0.90,
            "final_confidence": 0.95,
            "status": "fair",
            "evidence": ["checks/tests.stdout.txt"],
            "recovery_actions": ["Executed current source tests."],
            "remaining_limits": ["(-0.05) Native specialist receipts are synthetic."],
        },
    }
    path = _canonical_report(
        run, [], {}, "accept-as-is", confidence_metadata=metadata, retained_gates=json.loads(gates_bytes)
    )
    (run / "gates.json").write_bytes(gates_bytes)
    finder_spec = importlib.util.spec_from_file_location(
        "local_gate_finder", preparation.PLUGIN_ROOT / "shared/find-review-report.py"
    )
    finder = importlib.util.module_from_spec(finder_spec)
    sys.modules[finder_spec.name] = finder
    finder_spec.loader.exec_module(finder)
    assert finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent") == path
    sidecar_path = run / "checks/tests.python-imports.json"
    sidecar_bytes = sidecar_path.read_bytes()
    for problem in (
        "omit-one-worker",
        "omit-all-workers",
        "deleted-sidecar",
        "altered-sidecar",
        "altered-sidecar-rehashed",
        "invalid-sidecar",
        "missing-hash",
        "wrong-hash",
        "changed-inline",
    ):
        if not workers and problem.startswith("omit-"):
            continue
        changed_gates = json.loads(gates_bytes)
        gate = next(check for check in changed_gates["checks"] if check["id"] == "tests")
        if problem == "omit-one-worker":
            gate["python_imports"]["workers"].pop(next(iter(gate["python_imports"]["workers"])))
        elif problem == "omit-all-workers":
            gate["python_imports"]["workers"] = {}
        elif problem == "deleted-sidecar":
            sidecar_path.unlink()
        elif problem in {"altered-sidecar", "altered-sidecar-rehashed"}:
            changed_proof = json.loads(sidecar_bytes)
            changed_proof["status"] = "fail"
            sidecar_path.write_bytes(json.dumps(changed_proof).encode())
            if problem == "altered-sidecar-rehashed":
                gate["source"]["artifacts"]["checks/tests.python-imports.json"] = hashlib.sha256(
                    sidecar_path.read_bytes()
                ).hexdigest()
        elif problem == "invalid-sidecar":
            sidecar_path.write_bytes(b"{")
            gate["source"]["artifacts"]["checks/tests.python-imports.json"] = hashlib.sha256(b"{").hexdigest()
        elif problem == "missing-hash":
            gate["source"]["artifacts"].pop("checks/tests.python-imports.json", None)
        elif problem == "wrong-hash":
            gate["source"]["artifacts"]["checks/tests.python-imports.json"] = "0" * 64
        else:
            gate["python_imports"]["invoked_interpreter"] = str(tmp_path / "other-python")
        (run / "gates.json").write_bytes(json.dumps(changed_gates).encode())
        with pytest.raises(LookupError, match="local-confidence-tests-proof"):
            finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")
        sidecar_path.write_bytes(sidecar_bytes)
        (run / "gates.json").write_bytes(gates_bytes)
    for problem in (
        "external-test-unimported",
        "empty-worker",
        "failed-worker",
        "external-module-unimported",
        "missing-interpreter",
        "missing-prefix",
        "missing-module",
        "test-identity",
        "neutral-module",
    ):
        changed_gates = json.loads(gates_bytes)
        gate = next(check for check in changed_gates["checks"] if check["id"] == "tests")
        proof = gate["python_imports"]
        worker = {
            key: json.loads(json.dumps(proof[key]))
            for key in ("status", "runtime_interpreter", "sys_prefix", "worktree", "modules", "tests", "local_source")
        }
        module_name = next(iter(worker["modules"]))
        test_name = next(iter(worker["tests"]))
        if problem == "external-test-unimported":
            worker["status"] = "inconclusive"
            worker["tests"][test_name] = {
                "origin": str(tmp_path / "external.py"),
                "tracked": False,
                "status": "inconclusive",
                "reason": "module-not-imported",
            }
        elif problem == "empty-worker":
            worker["tests"] = {}
        elif problem == "failed-worker":
            worker["status"] = "fail"
        elif problem == "external-module-unimported":
            worker["status"] = "inconclusive"
            worker["modules"][module_name] = {
                "origin": str(tmp_path / "external.py"),
                "tracked": False,
                "status": "inconclusive",
                "reason": "module-not-imported",
            }
        elif problem == "missing-interpreter":
            worker.pop("runtime_interpreter")
        elif problem == "missing-prefix":
            worker.pop("sys_prefix")
        elif problem == "missing-module":
            worker["modules"].pop(module_name)
        elif problem == "test-identity":
            worker["tests"][str(tmp_path / "external.py")] = worker["tests"].pop(test_name)
        else:
            worker["status"] = "inconclusive"
            worker["modules"][module_name] = {
                "origin": None,
                "tracked": False,
                "status": "inconclusive",
                "reason": "module-not-imported",
            }
        proof["workers"] = {
            "gw0": worker,
            "gw1": {
                key: json.loads(json.dumps(proof[key]))
                for key in (
                    "status",
                    "runtime_interpreter",
                    "sys_prefix",
                    "worktree",
                    "modules",
                    "tests",
                    "local_source",
                )
            },
        }
        changed_sidecar = json.dumps(proof).encode()
        sidecar_path.write_bytes(changed_sidecar)
        gate["source"]["artifacts"]["checks/tests.python-imports.json"] = hashlib.sha256(changed_sidecar).hexdigest()
        (run / "gates.json").write_text(json.dumps(changed_gates), encoding="utf-8")
        if problem == "neutral-module":
            assert finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent") == path
        else:
            with pytest.raises(LookupError, match="local-source-tests"):
                finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")
    sidecar_path.write_bytes(sidecar_bytes)
    (run / "gates.json").write_bytes(gates_bytes)
    snapshot = json.loads((run / "checks/local-source-snapshot.json").read_bytes())
    gate_records = json.loads(gates_bytes)
    test_gate = next(check for check in gate_records["checks"] if check["id"] == "tests")
    snapshot["transport"] = test_gate["python_imports"]["local_source"]
    for problem in ("missing-member", "stale-member", "external-origin", "forged-tracked", "worker-snapshot"):
        changed_snapshot = json.loads(json.dumps(snapshot))
        changed_checks = json.loads(json.dumps(gate_records["checks"]))
        proof = next(check for check in changed_checks if check["id"] == "tests")["python_imports"]
        origin = next(iter(proof["tests"].values()))
        if problem == "missing-member":
            changed_snapshot["files"] = [
                record for record in changed_snapshot["files"] if record["path"] != "test_runtime.py"
            ]
        elif problem == "stale-member":
            origin["snapshot_member"]["sha256"] = "0" * 64
        elif problem == "external-origin":
            origin["origin"] = str(tmp_path / "repository/test_runtime.py")
        elif problem == "forged-tracked":
            assert origin["tracked"] is False
            origin["tracked"] = True
        else:
            worker = json.loads(json.dumps(proof))
            worker["local_source"]["snapshot_sha256"] = "0" * 64
            proof["workers"] = {"gw0": worker}
        with pytest.raises(SystemExit, match="(local-source-tests|pr-source-review-tests-import-proof-invalid)"):
            validator._validate_pr_tests_import_proof(changed_checks, mirror, changed_snapshot)
    with pytest.raises(SystemExit, match="pr-source-review-tests-import-proof-invalid"):
        validator._validate_pr_tests_import_proof(gate_records["checks"], mirror)
    release_spec = importlib.util.spec_from_file_location(
        "local_release_counterpart", preparation.PLUGIN_ROOT / "shared/validate-artifacts.py"
    )
    release_validator = importlib.util.module_from_spec(release_spec)
    sys.modules[release_spec.name] = release_validator
    release_spec.loader.exec_module(release_validator)
    head = subprocess.run(
        ["git", "-C", mirror, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    with pytest.raises(SystemExit, match="release-gate-source:lint:expected-head"):
        release_validator._validate_release_gate_source({"release_head": head}, gate_records)
    for status in ("fail", "not-applicable"):
        changed_gates = json.loads(gates_bytes)
        next(check for check in changed_gates["checks"] if check["id"] == "tests")["status"] = status
        (run / "gates.json").write_text(json.dumps(changed_gates), encoding="utf-8")
        assert "checks/tests.stdout.txt" not in validator._consolidation_confidence_evidence(run, manifest)
    (run / "gates.json").write_bytes(gates_bytes)
    original_log = (run / "checks/tests.stdout.txt").read_bytes()
    (run / "checks/tests.stdout.txt").write_bytes(original_log + b"forged extra execution\n")
    with pytest.raises(LookupError, match="local-confidence-tests-artifact-invalid"):
        finder.require_assessed_review_result(path, codex_home=home, parent_thread_id="parent")
    (run / "checks/tests.stdout.txt").write_bytes(original_log)
