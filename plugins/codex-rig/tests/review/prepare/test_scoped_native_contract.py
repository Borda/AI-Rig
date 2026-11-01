"""Exercise schema-bound native challenger response admission."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import test_review_prepare as preparation

_CONSUMER = {"id": "consumer", "source_paths": ["caller.py"], "claim": "Check the caller."}


@pytest.fixture(scope="module")
def validator() -> dict[str, object]:
    """Load the code-review artifact validator once; its namespace is only read by these tests."""
    return runpy.run_path(str(preparation.SKILL / "validate_artifacts.py"))


def _binding(*requirements: dict[str, object]) -> dict[str, object]:
    """Build the challenge-response binding that a native scoped dispatch hands to the validator."""
    return {
        "schema_version": 1,
        "evidence_schema_version": 3,
        "request_sha256": "c" * 64,
        "source_sha256": "a" * 64,
        "supporting_source_sha256": None,
        "diff_sha256": "b" * 64,
        "interaction_requirements": list(requirements),
    }


def _write_response(tmp_path: Path, interactions: list[dict[str, object]], rationale: str = "Reviewed.") -> Path:
    """Write a findings-free native challenger response carrying the given interaction verdicts."""
    response = tmp_path / "response.json"
    payload = {
        "source_sha256": "a" * 64,
        "diff_sha256": "b" * 64,
        "findings": [],
        "assessment": {"rating": 1, "rationale": rationale},
        "interactions": interactions,
    }
    response.write_text(json.dumps(payload), encoding="utf-8")
    return response


def _rate(validator: dict[str, object], response: Path, binding: dict[str, object] | None = None) -> object:
    """Read the retained rating of a structured native challenger response under an optional binding."""
    return validator["_retained_reviewer_rating"](
        response,
        local_reviewer_wave=False,
        main=False,
        role="challenger",
        structured_native=True,
        challenge_response=binding,
    )


class TestNativeScopedResponse:
    """Admission of schema-bound native challenger responses against their interaction bindings."""

    def test_requires_bound_interactions(self, validator: dict[str, object], tmp_path: Path) -> None:
        """A valid interaction shape is admitted only when the dispatch binding names the interaction.

        The same response is refused without a binding, since its interactions are then unverifiable, and admitted once
        the binding declares the matching `consumer` requirement.
        """
        response = _write_response(
            tmp_path,
            [{"id": "consumer", "verdict": "reviewed", "evidence": ["The caller passes its input."]}],
            "Reviewed the assigned source.",
        )

        with pytest.raises(SystemExit, match="review-assessment-content-invalid"):
            _rate(validator, response)

        assert _rate(validator, response, _binding(_CONSUMER)) == 1

    @pytest.mark.parametrize(
        ("damage", "value"),
        [
            pytest.param("evidence", "A string is not an evidence array.", id="evidence-string"),
            pytest.param("evidence", [], id="empty-evidence"),
            pytest.param("id", "other", id="different-interaction"),
            pytest.param("verdict", "approved", id="unknown-verdict"),
        ],
    )
    def test_rejects_malformed_interactions(
        self, validator: dict[str, object], tmp_path: Path, damage: str, value: object
    ) -> None:
        """An explicit binding does not admit changed identities or malformed reviewer evidence.

        One field of an otherwise valid `consumer` interaction is replaced by a non-array or empty evidence, a different
        interaction id, or an unknown verdict; each must fail admission.
        """
        item = {"id": "consumer", "verdict": "reviewed", "evidence": ["The caller forwards its input."]}
        item[damage] = value
        response = _write_response(tmp_path, [item])

        with pytest.raises(SystemExit, match="review-assessment-content-invalid"):
            _rate(validator, response, _binding(_CONSUMER))


def _inventory_response(tmp_path: Path, units: list[dict[str, object]]) -> tuple[Path, dict[str, object]]:
    """Write an inventory-audit response with the given unit verdicts and return it with its binding."""
    audit = {"id": "interaction-inventory-audit", "source_paths": ["caller.py"], "claim": "Audit the inventory."}
    binding = _binding({**audit, "unit_ids": ["unit-a", "unit-b"]})
    interaction = {"id": audit["id"], "verdict": "reviewed", "evidence": ["Inspected all units."]}
    return _write_response(tmp_path, [{**interaction, "unit_verdicts": units}]), binding


class TestNativeScopedInventoryAudit:
    """Each inventory unit requires its own ordered, well-typed verdict."""

    def test_accepts_every_unit_verdict(self, validator: dict[str, object], tmp_path: Path) -> None:
        """An audit that reviews both declared units in order is admitted.

        This is the baseline for the rejection cases below: the same response with both units intact must pass.
        """
        response, binding = _inventory_response(
            tmp_path, [{"id": "unit-a", "verdict": "reviewed"}, {"id": "unit-b", "verdict": "reviewed"}]
        )

        assert _rate(validator, response, binding) == 1

    @pytest.mark.parametrize(
        "units",
        [
            pytest.param(
                [{"id": "unit-a", "verdict": "reviewed"}, {"id": "unit-c", "verdict": "reviewed"}], id="wrong-unit"
            ),
            pytest.param([{"id": "unit-a", "verdict": "reviewed"}], id="missing-unit"),
            pytest.param(
                [{"id": "unit-a", "verdict": "reviewed"}, {"id": "unit-b", "verdict": None}], id="invalid-verdict"
            ),
        ],
    )
    def test_rejects_damaged_unit_verdicts(
        self, validator: dict[str, object], tmp_path: Path, units: list[dict[str, object]]
    ) -> None:
        """A renamed, missing or untyped unit verdict fails admission of the inventory audit.

        The binding declares `unit-a` and `unit-b`; a response naming `unit-c`, omitting `unit-b`, or giving it a null
        verdict does not prove the declared inventory was audited.
        """
        response, binding = _inventory_response(tmp_path, units)

        with pytest.raises(SystemExit, match="review-assessment-content-invalid"):
            _rate(validator, response, binding)


@dataclass
class _FrozenNativeRun:
    """A prepared scoped native challenge run together with the evidence its preflight reads."""

    run: Path
    gate_path: Path
    evidence: dict[str, object]
    evidence_path: Path
    support_path: Path
    requirements: list[dict[str, object]]


def _freeze_native_run(tmp_path: Path) -> _FrozenNativeRun:
    """Prepare a challenge-only paged run over real Git snapshots of one changed and one unchanged source."""
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
    (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8")
    briefs = {
        "challenger": {
            "axis": "Review the interaction.",
            "evidence_path": "brief.txt",
            "source_paths": ["stable.py", "widget.py"],
        }
    }
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
    command = [sys.executable, str(preparation.HELPER), "prepare", "--out", str(run), "--run-id", "test"]
    command += ["--parent-thread-id", "parent", "--source-root", str(repository), "--challenge-only", "--paged"]
    command += ["--challenge-evidence", str(evidence_path)]
    prepared = subprocess.run(command, capture_output=True, text=True, check=False)
    assert prepared.returncode == 0, prepared.stderr
    return _FrozenNativeRun(run, gate_path, evidence, evidence_path, tmp_path / "support.json", requirements)


def _preflight(frozen: _FrozenNativeRun) -> subprocess.CompletedProcess[str]:
    """Run the challenge-evidence preflight against the frozen run's inspection plan."""
    command = [sys.executable, str(frozen.gate_path), "--preflight-plan", str(frozen.run / "inspection-plan.json")]
    command += ["--request-evidence", str(frozen.evidence_path), "--supporting-source", str(frozen.support_path)]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _damage_dispatch(frozen: _FrozenNativeRun) -> None:
    """Swap the source digest in the issued dispatch message for an unrelated one."""
    plan = json.loads((frozen.run / "inspection-plan.json").read_bytes())
    dispatch_path = frozen.run / "dispatch.json"
    dispatch = json.loads(dispatch_path.read_bytes())
    arguments = dispatch["calls"][0]["arguments"]
    arguments["message"] = arguments["message"].replace(plan["challenge_response"]["source_sha256"], "0" * 64)
    dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")


def _damage_request(frozen: _FrozenNativeRun) -> None:
    """Change the goal in the request evidence after the plan was frozen."""
    frozen.evidence["request"]["goal"] = "A different goal."
    frozen.evidence_path.write_text(json.dumps(frozen.evidence), encoding="utf-8")


@pytest.mark.integration
def test_scoped_native_preparation_and_preflight_bind_real_sources(tmp_path: Path) -> None:
    """A frozen native plan binds actual Git snapshots, request and issued dispatch through to a valid manifest.

    A challenge-only paged run is prepared over a changed and an unchanged source with an interaction requirement. The
    plan, dispatch and preflight must agree, and a compliant response must assemble and validate end to end.
    """
    frozen = _freeze_native_run(tmp_path)
    run = frozen.run
    plan = json.loads((run / "inspection-plan.json").read_bytes())
    message = json.loads((run / "dispatch.json").read_bytes())["calls"][0]["arguments"]["message"]
    response = {
        "source_sha256": plan["challenge_response"]["source_sha256"],
        "diff_sha256": plan["challenge_response"]["diff_sha256"],
        "findings": [],
        "assessment": {"rating": 1, "rationale": "Inspected both sources."},
        "interactions": [{"id": "consumer", "verdict": "reviewed", "evidence": ["Inspected both sources."]}],
    }

    preflight = _preflight(frozen)

    assert plan["challenge_response"]["interaction_requirements"] == frozen.requirements
    assert plan["challenge_response"]["source_sha256"] in message
    assert plan["challenge_response"]["diff_sha256"] in message
    assert "nonempty array of nonempty strings" in message
    assert preflight.returncode == 0, preflight.stderr
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
    command = [sys.executable, str(preparation.SKILL / "validate_artifacts.py"), "--out", str(run), "--manifest-only"]
    command += ["--challenge-only", "--codex-home", str(home), "--parent-thread-id", "parent"]
    command += ["--project-root", str(preparation.PLUGIN_ROOT.parents[1])]
    validated = subprocess.run(command, capture_output=True, text=True, check=False)
    assert validated.returncode == 0, validated.stderr


@pytest.mark.integration
@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(_damage_dispatch, id="modified-dispatch"),
        pytest.param(_damage_request, id="modified-request"),
    ],
)
def test_scoped_native_preflight_rejects_changed_evidence(
    tmp_path: Path, damage: Callable[[_FrozenNativeRun], None]
) -> None:
    """Preflight refuses a frozen plan whose issued dispatch or request evidence was altered afterwards.

    The prepared run is valid until either the dispatch digest or the request goal changes; the preflight must then fail
    with a loop-evidence diagnostic instead of admitting evidence that no longer matches the plan.
    """
    frozen = _freeze_native_run(tmp_path)
    damage(frozen)

    preflight = _preflight(frozen)

    assert preflight.returncode != 0
    assert "loop-evidence-preflight-" in preflight.stderr
