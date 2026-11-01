"""Bind isolated reviewer evidence to the canonical review acceptance path."""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _artifact_files import read_json, write_json

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = PLUGIN_ROOT / "skills/code-review/validate_artifacts.py"
FINDER_PATH = PLUGIN_ROOT / "shared/find-review-report.py"
PARENT_THREAD = "thread"


def _module(path: Path) -> ModuleType:
    """Load existing installed helpers and test-only artifact construction."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.cache
def _sibling_test_module(name: str) -> ModuleType:
    """Load a review-suite test module once; its artifact constructors are pure functions of their arguments.

    ``name`` is relative to the review test directory, so modules in other topic folders resolve too.
    """
    return _module(Path(__file__).parents[1] / name)


def _sha256(path: Path) -> str:
    """Return the SHA-256 of a file's current bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class _Wave:
    """Specialist manifest plus the app-server plan and evidence it binds by digest."""

    manifest_path: Path
    manifest: dict
    plan_path: Path
    plan: dict
    evidence_path: Path
    evidence: dict

    def save(self) -> None:
        """Persist plan, evidence and manifest, re-binding each digest to the bytes just written."""
        write_json(self.plan_path, self.plan)
        self.evidence["plan_sha256"] = _sha256(self.plan_path)
        write_json(self.evidence_path, self.evidence)
        self.manifest["app_server_execution"]["evidence_sha256"] = _sha256(self.evidence_path)
        write_json(self.manifest_path, self.manifest)


def _load_wave(run: Path) -> _Wave:
    """Read the wave artifacts of a synthetic review run."""
    manifest_path = run / "specialist-manifest.json"
    manifest = read_json(manifest_path)
    plan_path = Path(manifest["app_server_execution"]["plan_path"])
    evidence_path = Path(manifest["app_server_execution"]["evidence_path"])
    return _Wave(manifest_path, manifest, plan_path, read_json(plan_path), evidence_path, read_json(evidence_path))


def _keep_wave_nodes(wave: _Wave, roles: tuple[str, ...]) -> None:
    """Drop every reviewer outside `roles` from the plan and evidence, leaving manifest passes untouched."""
    wave.plan["nodes"] = [node for node in wave.plan["nodes"] if node["role_id"] in roles]
    wave.evidence["nodes"] = [node for node in wave.evidence["nodes"] if node["role_id"] in roles]


def _keep_roles(wave: _Wave, roles: tuple[str, ...]) -> None:
    """Drop every reviewer outside `roles` from plan, evidence and manifest passes."""
    _keep_wave_nodes(wave, roles)
    wave.manifest["passes"] = [item for item in wave.manifest["passes"] if item["role"] in roles]


def _downgrade_to_legacy_plan(plan: dict) -> None:
    """Rewrite a current dispatch plan as schema version 1, dropping the fields only current dispatch records."""
    plan["schema_version"] = 1
    for key in ("source_path", "source_sha256", "diff_path", "diff_sha256"):
        del plan[key]
    for node in plan["nodes"]:
        del node["capacity_receipt"]


def _update_matching(items: list[dict], key: str, value: str, **changes: Any) -> None:
    """Apply `changes` to every item whose `key` equals `value`."""
    for item in items:
        if item[key] == value:
            item.update(changes)


def _complete_run(run: Path) -> subprocess.CompletedProcess[bytes]:
    """Run the canonical completion gate for a review run in a real subprocess."""
    command = [sys.executable, str(FINDER_PATH), "--complete-run", str(run), "--parent-thread-id", PARENT_THREAD]
    return subprocess.run(command, capture_output=True, timeout=30, check=False)


def _challenge_only(run: Path) -> subprocess.CompletedProcess[str]:
    """Run the manifest-only challenger validation in a real subprocess."""
    command = [
        sys.executable,
        str(VALIDATOR_PATH),
        *("--out", str(run), "--manifest-only", "--challenge-only", "--parent-thread-id", PARENT_THREAD),
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)


@pytest.fixture
def isolated_review(tmp_path: Path, text_newline_default: None) -> Path:
    """Build a full synthetic HIGH_RISK review through existing artifact constructors.

    Another suite calls ``isolated_review.__wrapped__(path, text_newline_default=None)`` directly, so the parameter
    names stay fixed.
    """
    completion_tests = _sibling_test_module("validation/test_review_completion_gate.py")
    run = completion_tests._assessed_pr.__wrapped__(tmp_path)
    evidence_tests = _sibling_test_module("reviewers/test_local_reviewer_wave.py")
    plan_path, evidence_path = evidence_tests.review_evidence_files(
        run, roles=("qa-specialist", "challenger"), source_path="widget.txt"
    )
    result_path = run / "result.json"
    result = read_json(result_path)
    metadata = result["metadata"]
    plan = read_json(plan_path)
    plan["source_rating_version"] = 1
    evidence = read_json(evidence_path)
    for node in evidence["nodes"]:
        node["started_at_ms"] = 0
        node["finished_at_ms"] = 15
        role = node["role_id"]
        retained_card = run / "role-cards" / role / "ROLE.md"
        retained_card.parent.mkdir(parents=True, exist_ok=True)
        retained_card.write_bytes((PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes())
        output = evidence_path.parent / node["output_path"]
        write_json(
            output,
            {
                "source_sha256": plan["source_sha256"],
                "diff_sha256": plan["review_input_sha256"],
                "assessment": {
                    "source_rating_version": 1,
                    "rating": 1,
                    "verification_readiness": "complete",
                    "rationale": "The inspected scope is clean.",
                },
                "findings": [],
            },
        )
        node["output_sha256"] = _sha256(output)
    for payload in (plan, evidence):
        payload.update(
            review_run_id=metadata["review_run_id"],
            parent_thread_id=PARENT_THREAD,
            review_input_sha256=metadata["review_input_sha256"],
        )
    write_json(plan_path, plan)
    evidence["plan_sha256"] = _sha256(plan_path)
    write_json(evidence_path, evidence)
    passes = [
        {
            "role": node["role_id"],
            "role_card_sha256": node["role_card_sha256"],
            "mode": "app-server",
            "axis": "independent verification",
            "trigger": "HIGH_RISK",
            "confidence": 0.95,
            "blocking_findings": 0,
            "output_path": str(evidence_path.parent / node["output_path"]),
        }
        for node in evidence["nodes"]
    ]
    manifest = {
        "schema_version": 4,
        "review_run_id": metadata["review_run_id"],
        "parent_thread_id": PARENT_THREAD,
        "review_input_sha256": metadata["review_input_sha256"],
        "passes": passes,
        "app_server_execution": {
            "plan_path": str(plan_path),
            "evidence_path": str(evidence_path),
            "evidence_sha256": _sha256(evidence_path),
        },
    }
    write_json(run / "specialist-manifest.json", manifest)
    routing_path = run / "review-routing.json"
    routing = read_json(routing_path)
    routing["risk_tier"] = "HIGH_RISK"
    routing["signals"].update(high_candidate=True, behavior_change=True)
    routing["triggered_roles"] = sorted(item["role"] for item in passes)
    routing["trigger_reasons"] = {item["role"]: ["HIGH_RISK fixture"] for item in passes}
    write_json(routing_path, routing)
    metadata.update(
        risk_tier="HIGH_RISK",
        specialist_passes=passes,
        independence_required=True,
        independence_satisfied=True,
        execution_mode="parallel",
        execution_evidence_level="app-server-parent-observed",
        write_parallel_eligible=False,
    )
    metadata["reviewer_assessments"].extend(
        {
            "role": "QA specialist" if item["role"] == "qa-specialist" else "Challenger",
            "rating": 1,
            "evidence": item["output_path"],
        }
        for item in passes
    )
    handoff_path = run / "final-handoff.json"
    handoff = read_json(handoff_path)
    handoff["tables"][0]["reviewers"] = metadata["reviewer_assessments"]
    write_json(handoff_path, handoff)
    binding = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, run / "final.md", run / "final-handoff.validation.json"
    )
    metadata["final_handoff"].update({key: binding[key] for key in ("handoff_sha256", "rendered_sha256")})
    write_json(result_path, result)
    return run


def _forge_challenger_output(manifest: dict) -> None:
    """Overwrite the retained challenger output with text the evidence receipt never covered."""
    Path(manifest["passes"][0]["output_path"]).write_text("Forged reviewer output\n", encoding="utf-8", newline="\n")


def _leave_unchanged(manifest: dict) -> None:
    """Leave the retained reviewer output exactly as the wave produced it."""


def _replace_response(run: Path, manifest: dict) -> None:
    """Replace the first reviewer's retained final response."""
    Path(manifest["passes"][0]["output_path"]).write_bytes(b"Replaced final response.\n")


def _replace_input(run: Path, manifest: dict) -> None:
    """Replace the diff the reviewers were asked to inspect."""
    (run / "diff.patch").write_bytes(b"Replaced input.\n")


def _forge_execution_digest(run: Path, manifest: dict) -> None:
    """Point the manifest at an evidence digest that matches no file."""
    manifest["app_server_execution"]["evidence_sha256"] = "0" * 64


def _add_native_attempt(run: Path, manifest: dict) -> None:
    """Attach a native-attempt record to a pass that must come from the app-server wave."""
    manifest["passes"][0]["attempts"] = [{"status": "completed"}]


def _drop_source_digest(output: dict) -> None:
    """Remove the source digest the reviewer turn contract requires."""
    del output["source_sha256"]


def _drop_diff_digest(output: dict) -> None:
    """Remove the diff digest the reviewer turn contract requires."""
    del output["diff_sha256"]


def _use_boolean_rating(output: dict) -> None:
    """Replace the numeric rating with a boolean, which is not a valid rating."""
    output["assessment"]["rating"] = True


def _add_unsupported_finding(output: dict) -> None:
    """Add a finding that lacks every required field."""
    output["findings"] = [{"signature": "unsupported"}]


def test_local_reviewer_manifest_requires_execution_evidence(tmp_path: Path) -> None:
    """A manifest declaration alone does not admit the independent reviewer route.

    The manifest names no app-server execution and no passes; the validator must demand execution evidence rather than
    trust the declaration.
    """
    review_input = b"diff --git a/widget.py b/widget.py\n"
    (tmp_path / "diff.patch").write_bytes(review_input)
    manifest = {
        "schema_version": 4,
        "review_run_id": "isolated-review",
        "parent_thread_id": "parent-correlation",
        "review_input_sha256": hashlib.sha256(review_input).hexdigest(),
        "passes": [],
    }
    validator = _module(VALIDATOR_PATH)

    with pytest.raises(SystemExit, match="review-app-server-execution-missing"):
        validator._validate_manifest_entries(tmp_path, manifest, [], set(), tmp_path, "parent-correlation", tmp_path)


class TestHighRiskWave:
    """A HIGH_RISK review backed by two overlapping, authenticated reviewer threads."""

    def test_completion_gate_prints_final_handoff(self, isolated_review: Path) -> None:
        """Both canonical validators accept the wave and the gate prints the retained final handoff.

        The fixture holds genuinely distinct reviewer threads that overlap in time, so the completion gate must exit
        zero and emit exactly the bytes of the run's rendered final handoff.
        """
        completed = _complete_run(isolated_review)

        assert completed.returncode == 0, completed.stderr.decode()
        assert completed.stdout == (isolated_review / "final.md").read_bytes()

    def test_report_lookup_finds_result(self, isolated_review: Path) -> None:
        """Report lookup by PR target resolves to the run's result file.

        The run sits under a reports directory; the lookup CLI must print the path of its `result.json`.
        """
        command = [
            sys.executable,
            str(FINDER_PATH),
            "--target",
            "#123",
            "--reports-dir",
            str(isolated_review.parent.parent),
        ]

        lookup = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)

        assert lookup.returncode == 0, lookup.stderr
        assert Path(lookup.stdout.strip()) == isolated_review / "result.json"

    def test_rejects_serial_specialists(self, isolated_review: Path) -> None:
        """Two reviewers that completed without overlapping cannot pass an assessed review.

        Each reviewer finished independently, but the second started after the first ended. An assessed review requires
        the parallel wave, so the result validator must refuse it.
        """
        wave = _load_wave(isolated_review)
        wave.evidence["nodes"][1]["started_at_ms"] = 20
        wave.evidence["nodes"][1]["finished_at_ms"] = 30
        wave.save()
        result_path = isolated_review / "result.json"
        result = read_json(result_path)
        result["metadata"]["execution_mode"] = "independent-spawned"
        write_json(result_path, result)
        validator = _module(VALIDATOR_PATH)

        with pytest.raises(SystemExit, match="parallel-review-required-for-pass"):
            validator._validate_result(isolated_review, result_path, isolated_review, PARENT_THREAD, isolated_review)

    def test_rejects_legacy_plan_dispatch(self, isolated_review: Path) -> None:
        """Historical adapter evidence stays readable but cannot satisfy a new review run.

        The plan is downgraded to schema version 1 and stripped of the source, diff and capacity fields that current
        dispatch records. The wave validator must name the legacy dispatch as the reason for rejection.
        """
        wave = _load_wave(isolated_review)
        _downgrade_to_legacy_plan(wave.plan)
        wave.save()
        validator = _module(VALIDATOR_PATH)

        with pytest.raises(SystemExit, match="review-app-server-evidence-invalid:plan-legacy-dispatch-forbidden"):
            validator._validate_local_reviewer_wave(isolated_review, wave.manifest, wave.manifest["passes"])

    @pytest.mark.parametrize(
        "tamper",
        [
            pytest.param(_replace_response, id="response"),
            pytest.param(_replace_input, id="input"),
            pytest.param(_forge_execution_digest, id="execution-digest"),
            pytest.param(_add_native_attempt, id="native-attempt"),
        ],
    )
    def test_completion_rejects_changed_evidence(self, isolated_review: Path, tamper: Callable[..., None]) -> None:
        """Final completion rejects evidence changed after the adapter validated it.

        Each case alters one retained artifact (response, input, execution digest, or a native-attempt record) and then
        runs the completion gate, which must block the handoff and print nothing.
        """
        manifest_path = isolated_review / "specialist-manifest.json"
        manifest = read_json(manifest_path)
        tamper(isolated_review, manifest)
        write_json(manifest_path, manifest)

        completed = _complete_run(isolated_review)

        assert completed.returncode == 1
        assert b"Review handoff blocked:" in completed.stderr
        assert not completed.stdout

    @pytest.mark.parametrize(
        "corrupt",
        [
            pytest.param(_drop_source_digest, id="missing-source-digest"),
            pytest.param(_drop_diff_digest, id="missing-diff-digest"),
            pytest.param(_use_boolean_rating, id="invalid-rating"),
            pytest.param(_add_unsupported_finding, id="invalid-finding"),
        ],
    )
    def test_completion_rejects_rehashed_invalid_output(
        self, isolated_review: Path, corrupt: Callable[..., None]
    ) -> None:
        """A self-consistent receipt cannot authorize reviewer JSON that violates the turn contract.

        The corrupted output is re-hashed into the evidence and manifest, so every digest matches. Only the output's
        content is wrong, and the completion gate must still reject it.
        """
        wave = _load_wave(isolated_review)
        node = wave.evidence["nodes"][0]
        output_path = wave.evidence_path.parent / node["output_path"]
        output = read_json(output_path)
        corrupt(output)
        write_json(output_path, output)
        node["output_sha256"] = _sha256(output_path)
        wave.save()

        completed = _complete_run(isolated_review)

        assert completed.returncode == 1
        assert b"review-app-server-evidence-invalid:app-server-review-output-" in completed.stderr
        assert not completed.stdout

    def test_rejects_parent_text_in_place_of_required_thread(self, isolated_review: Path) -> None:
        """A valid challenger wave cannot lend its independence to a substituted QA review.

        The QA specialist's wave node is removed and its pass is replaced by parent-authored text. The result validator
        must demand an independent QA review for the pass.
        """
        wave = _load_wave(isolated_review)
        _keep_wave_nodes(wave, ("challenger",))
        substitute_path = isolated_review / "qa-specialist-parent.md"
        substitute_path.write_text(
            "role_id: qa-specialist\n## Reviewer Assessment\n\nSource rating version: 1\nRating: 3\n"
            "Verification readiness: complete\nRationale: Parent inspection cannot establish independent QA.\n",
            encoding="utf-8",
            newline="\n",
        )
        _update_matching(
            wave.manifest["passes"], "role", "qa-specialist", mode="substituted", output_path=str(substitute_path)
        )
        wave.save()
        result_path = isolated_review / "result.json"
        result = read_json(result_path)
        result["metadata"].update(
            specialist_passes=wave.manifest["passes"],
            fanout_substituted=True,
            independence_satisfied=False,
            execution_mode="independent-spawned",
        )
        _update_matching(
            result["metadata"]["reviewer_assessments"],
            "role",
            "QA specialist",
            role="QA specialist (parent substitute)",
            rating=3,
            evidence="qa-specialist-parent.md",
        )
        write_json(result_path, result)
        validator = _module(VALIDATOR_PATH)

        with pytest.raises(SystemExit, match="independent-review-required-for-pass:qa-specialist"):
            validator._validate_result(isolated_review, result_path, isolated_review, PARENT_THREAD, isolated_review)


class TestChallengeManifest:
    """Challenge-only validation admits exactly one authenticated challenger."""

    def test_accepts_one_challenger(self, isolated_review: Path) -> None:
        """One verified challenger passes challenge-only validation independently of generic Code Review routing.

        The QA specialist is removed from plan, evidence and manifest, leaving the challenger as the only reviewer.
        """
        wave = _load_wave(isolated_review)
        _keep_roles(wave, ("challenger",))
        wave.save()

        completed = _challenge_only(isolated_review)

        assert completed.returncode == 0, completed.stderr

    @pytest.mark.parametrize(
        ("roles", "tamper", "expected_error"),
        [
            pytest.param(("qa-specialist",), _leave_unchanged, "manifest-triggered-role-set-mismatch", id="wrong-role"),
            pytest.param(
                ("qa-specialist", "challenger"),
                _leave_unchanged,
                "manifest-triggered-role-set-mismatch",
                id="extra-role",
            ),
            pytest.param(
                ("challenger",), _forge_challenger_output, "evidence-output-sha256-mismatch", id="forged-output"
            ),
        ],
    )
    def test_rejects_other_reviewer_sets_and_forged_output(
        self, isolated_review: Path, roles: tuple[str, ...], tamper: Callable[[dict], None], expected_error: str
    ) -> None:
        """Challenge-only validation rejects a non-challenger role set and an unauthenticated challenger output.

        The wave is narrowed to `roles`; the forged case also rewrites the retained output after the receipt was signed.
        The validator must fail with the matching error code.
        """
        wave = _load_wave(isolated_review)
        _keep_roles(wave, roles)
        wave.save()
        tamper(wave.manifest)

        completed = _challenge_only(isolated_review)

        assert completed.returncode != 0
        assert expected_error in completed.stderr
