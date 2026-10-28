"""Exercise schema-bound native challenger response admission."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest
import test_review_prepare as preparation


def test_native_scoped_response_requires_bound_interactions(tmp_path: Path) -> None:
    """A real consumer's valid interaction shape must survive native admission."""
    validator = runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))
    response = tmp_path / "response.json"
    payload = {
        "source_sha256": "a" * 64,
        "diff_sha256": "b" * 64,
        "findings": [],
        "assessment": {"rating": 1, "rationale": "Reviewed the assigned source."},
        "interactions": [{"id": "consumer", "verdict": "reviewed", "evidence": ["The caller passes its input."]}],
    }
    response.write_text(json.dumps(payload), encoding="utf-8")
    requirements = [{"id": "consumer", "source_paths": ["caller.py"], "claim": "Check the caller."}]
    rate = validator["_retained_reviewer_rating"]
    with pytest.raises(SystemExit, match="review-assessment-content-invalid"):
        rate(response, local_reviewer_wave=False, main=False, role="challenger", structured_native=True)
    assert (
        rate(
            response,
            local_reviewer_wave=False,
            main=False,
            role="challenger",
            structured_native=True,
            challenge_response={
                "schema_version": 1,
                "evidence_schema_version": 3,
                "request_sha256": "c" * 64,
                "source_sha256": "a" * 64,
                "supporting_source_sha256": None,
                "diff_sha256": "b" * 64,
                "interaction_requirements": requirements,
            },
        )
        == 1
    )


@pytest.mark.parametrize(
    ("damage", "value"),
    [
        pytest.param("evidence", "A string is not an evidence array.", id="evidence-string"),
        pytest.param("evidence", [], id="empty-evidence"),
        pytest.param("id", "other", id="different-interaction"),
        pytest.param("verdict", "approved", id="unknown-verdict"),
    ],
)
def test_native_scoped_response_rejects_malformed_interactions(tmp_path: Path, damage: str, value: object) -> None:
    """An explicit binding does not admit changed identities or malformed reviewer evidence."""
    validator = runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))
    item = {"id": "consumer", "verdict": "reviewed", "evidence": ["The caller forwards its input."]}
    item[damage] = value
    response = tmp_path / "response.json"
    response.write_text(
        json.dumps(
            {
                "source_sha256": "a" * 64,
                "diff_sha256": "b" * 64,
                "findings": [],
                "assessment": {"rating": 1, "rationale": "Reviewed."},
                "interactions": [item],
            }
        ),
        encoding="utf-8",
    )
    binding = {
        "schema_version": 1,
        "evidence_schema_version": 3,
        "request_sha256": "c" * 64,
        "source_sha256": "a" * 64,
        "supporting_source_sha256": None,
        "diff_sha256": "b" * 64,
        "interaction_requirements": [{"id": "consumer", "source_paths": ["caller.py"], "claim": "Check the caller."}],
    }
    with pytest.raises(SystemExit, match="review-assessment-content-invalid"):
        validator["_retained_reviewer_rating"](
            response,
            local_reviewer_wave=False,
            main=False,
            role="challenger",
            structured_native=True,
            challenge_response=binding,
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "damage",
    [
        pytest.param("none", id="valid"),
        pytest.param("dispatch", id="modified-dispatch"),
        pytest.param("request", id="modified-request"),
    ],
)
def test_scoped_native_preparation_and_preflight_bind_real_sources(tmp_path: Path, damage: str) -> None:
    """A frozen native plan binds actual Git snapshots, request, and the issued dispatch."""
    import subprocess
    import sys

    run = preparation._review_inputs(tmp_path, unchanged_caller=True)
    repository = Path(json.loads((run / "local-source/review-worktree.json").read_bytes())["review_worktree"])
    gate_path = preparation.PLUGIN_ROOT / "skills/challenge-resolve/validate_evidence.py"
    gate = runpy.run_path(str(gate_path))
    source = gate["_canonical_source_bytes"](gate["capture_source_snapshot"](repository, ["widget.py"]))
    support = gate["_canonical_source_bytes"](gate["capture_source_snapshot"](repository, ["stable.py"]))
    (tmp_path / "source.json").write_bytes(source)
    (tmp_path / "support.json").write_bytes(support)
    requirements = [{"id": "consumer", "source_paths": ["stable.py", "widget.py"], "claim": "Inspect both sources."}]
    request = {
        "goal": "Review the sources.",
        "specification": "Check behavior.\nInteraction assessment:\n"
        + json.dumps(requirements, sort_keys=True, ensure_ascii=True),
        "done_when": "Both sources are assessed.",
    }
    evidence = {
        "schema_version": 3,
        "repository": repository.as_posix(),
        "scope_paths": ["widget.py"],
        "current_source_path": "source.json",
        "supporting_paths": ["stable.py"],
        "current_supporting_source_path": "support.json",
        "request": request,
        "origin": {"kind": "first-run", "caller_run": None, "prior_ledger_sha256": None},
        "interaction_requirements": requirements,
        "rounds": [],
    }
    evidence_path = tmp_path / "loop-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    diff = (run / "diff.patch").read_bytes()
    envelope = (
        b"Review request:\n"
        + json.dumps(request, sort_keys=True, ensure_ascii=True).encode()
        + b"\nFrozen source:\n"
        + source
        + b"\nFrozen diff:\n"
        + diff
        + b"\nSupporting source:\n"
        + support
        + b"\n"
    )
    (run / "brief.txt").write_bytes(envelope)
    routing = json.loads((run / "review-routing.json").read_bytes())
    routing.update(
        triggered_roles=["challenger"],
        trigger_reasons={"challenger": ["Inspect the declared interaction."]},
        independent_review_required=True,
        independence_requirement_evidence="Independent review.",
    )
    (run / "review-routing.json").write_text(
        json.dumps(routing),
        encoding="utf-8",
    )
    (run / "review-briefs.json").write_text(
        json.dumps(
            {
                "challenger": {
                    "axis": "Review the interaction.",
                    "evidence_path": "brief.txt",
                    "source_paths": ["stable.py", "widget.py"],
                }
            }
        ),
        encoding="utf-8",
    )
    args = [
        sys.executable,
        str(preparation.HELPER),
        "prepare",
        "--out",
        str(run),
        "--run-id",
        "test",
        "--parent-thread-id",
        "parent",
        "--source-root",
        str(repository),
        "--challenge-only",
        "--paged",
        "--challenge-evidence",
        str(evidence_path),
    ]
    prepared = subprocess.run(args, capture_output=True, text=True, check=False)
    assert prepared.returncode == 0, prepared.stderr
    plan = json.loads((run / "inspection-plan.json").read_bytes())
    assert plan["challenge_response"]["interaction_requirements"] == requirements
    dispatch_path = run / "dispatch.json"
    dispatch = json.loads(dispatch_path.read_bytes())
    message = dispatch["calls"][0]["arguments"]["message"]
    assert plan["challenge_response"]["source_sha256"] in message
    assert plan["challenge_response"]["diff_sha256"] in message
    assert "nonempty array of nonempty strings" in dispatch["calls"][0]["arguments"]["message"]
    if damage == "dispatch":
        dispatch["calls"][0]["arguments"]["message"] = message.replace(
            plan["challenge_response"]["source_sha256"], "0" * 64
        )
        dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")
    elif damage == "request":
        evidence["request"]["goal"] = "A different goal."
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    preflight = subprocess.run(
        [
            sys.executable,
            str(gate_path),
            "--preflight-plan",
            str(run / "inspection-plan.json"),
            "--request-evidence",
            str(evidence_path),
            "--supporting-source",
            str(tmp_path / "support.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if damage == "none":
        assert preflight.returncode == 0, preflight.stderr
        response = {
            "source_sha256": plan["challenge_response"]["source_sha256"],
            "diff_sha256": plan["challenge_response"]["diff_sha256"],
            "findings": [],
            "assessment": {"rating": 1, "rationale": "Inspected both sources."},
            "interactions": [{"id": "consumer", "verdict": "reviewed", "evidence": ["Inspected both sources."]}],
        }
        _, home, _ = preparation._assembly_evidence(
            tmp_path,
            prepared_run=run,
            findings={"challenger": json.dumps(response)},
            final_header="missing",
            compact_reader=True,
        )
        assembled = preparation._assemble(run, home, challenge_only=True)
        assert assembled.returncode == 0, assembled.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        assert [item["role"] for item in manifest["passes"]] == ["challenger"]
        assert json.loads((run / manifest["passes"][0]["output_path"]).read_bytes()) == response
        validated = subprocess.run(
            [
                sys.executable,
                str(preparation.SKILL / "validate_artifacts.py"),
                "--out",
                str(run),
                "--manifest-only",
                "--challenge-only",
                "--codex-home",
                str(home),
                "--parent-thread-id",
                "parent",
                "--project-root",
                str(preparation.PLUGIN_ROOT.parents[1]),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert validated.returncode == 0, validated.stderr
    else:
        assert preflight.returncode != 0
        assert "loop-evidence-preflight-" in preflight.stderr


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param("none", id="valid-audit"),
        pytest.param("unit", id="wrong-unit"),
        pytest.param("missing", id="missing-unit"),
        pytest.param("type", id="invalid-unit-verdict"),
    ],
)
def test_native_scoped_inventory_audit_preserves_unit_verdicts(tmp_path: Path, damage: str) -> None:
    """Each inventory unit requires its own ordered, well-typed verdict."""
    validator = runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))
    binding = {
        "schema_version": 1,
        "evidence_schema_version": 3,
        "request_sha256": "c" * 64,
        "source_sha256": "a" * 64,
        "supporting_source_sha256": None,
        "diff_sha256": "b" * 64,
        "interaction_requirements": [
            {
                "id": "interaction-inventory-audit",
                "source_paths": ["caller.py"],
                "claim": "Audit the inventory.",
                "unit_ids": ["unit-a", "unit-b"],
            }
        ],
    }
    units = [{"id": "unit-a", "verdict": "reviewed"}, {"id": "unit-b", "verdict": "reviewed"}]
    if damage == "unit":
        units[1]["id"] = "unit-c"
    elif damage == "missing":
        units.pop()
    elif damage == "type":
        units[1]["verdict"] = None
    payload = {
        "source_sha256": "a" * 64,
        "diff_sha256": "b" * 64,
        "findings": [],
        "assessment": {"rating": 1, "rationale": "Reviewed."},
        "interactions": [
            {
                "id": "interaction-inventory-audit",
                "verdict": "reviewed",
                "evidence": ["Inspected all units."],
                "unit_verdicts": units,
            }
        ],
    }
    response = tmp_path / "response.json"
    response.write_text(json.dumps(payload), encoding="utf-8")
    rate = validator["_retained_reviewer_rating"]
    args = {
        "local_reviewer_wave": False,
        "main": False,
        "role": "challenger",
        "structured_native": True,
        "challenge_response": binding,
    }
    if damage == "none":
        assert rate(response, **args) == 1
    else:
        with pytest.raises(SystemExit, match="review-assessment-content-invalid"):
            rate(response, **args)
