"""Regression coverage for terminal PR close decisions."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_REVIEW_SKILL = PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md"
REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
SHARED_VALIDATOR = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
WRITE_RESULT = PLUGIN_ROOT / "shared" / "write-result.py"
FINALIZER = PLUGIN_ROOT / "shared" / "final_handoff.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")
HEAD_OID = "b" * 40
BASE_OID = "a" * 40
CLOSE_CODES = (
    "FALSE_GOAL",
    "BREAKING_CONDUCT",
    "WRONG_SCOPE",
    "WRONG_PROVENANCE",
    "DUPLICATE",
    "UNADDRESSED_REVERT",
    "SPAM",
    "ARCHITECTURE_VIOLATION",
)
CONFIDENCE_GAP = "Detailed source review was intentionally skipped after the close gate."


def _load_validator() -> object:
    """Load the standalone review validator from its shipped location."""
    specification = importlib.util.spec_from_file_location("code_review_close_validator", REVIEW_VALIDATOR)
    assert specification is not None and specification.loader is not None
    validator = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(validator)
    return validator


def _load_shared_validator() -> object:
    """Load the shared artifact validator used after the review-specific gate."""
    specification = importlib.util.spec_from_file_location("shared_close_validator", SHARED_VALIDATOR)
    assert specification is not None and specification.loader is not None
    validator = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(validator)
    return validator


def _write_closed_final_handoff(out_dir: Path, result_path: Path, metadata: dict[str, object]) -> dict[str, object]:
    """Render the terminal close response and return its result metadata binding."""
    gates = json.loads((out_dir / "gates.json").read_text(encoding="utf-8"))
    recovery = metadata["confidence_recovery"]
    assert isinstance(recovery, dict)
    closures = metadata["confidence_gap_closures"]
    assert isinstance(closures, list)
    handoff = {
        "schema_version": 1,
        "skill": "code-review",
        "branch": "closed",
        "outcome": {"title": "Review Decision", "summary": "Close is advised; source findings were not assessed."},
        "tables": [],
        "source_records": [],
        "source_coverage": {
            "source_records_total": 0,
            "represented_source_records_total": 0,
            "omitted_source_records_total": 0,
        },
        "verification": [
            {"check": check["id"], "status": check["status"], "evidence": check["stdout"]} for check in gates["checks"]
        ],
        "remaining": [],
        "next_steps": [],
        "confidence": {
            "score": 0.95,
            "band": "fair",
            "limits": recovery["remaining_limits"],
            "gaps": closures,
        },
        "artifacts": [{"label": "Result", "path": str(result_path)}],
        "caller_contract": None,
    }
    handoff_path = out_dir / "final-handoff.json"
    final_path = out_dir / "final.md"
    validation_path = out_dir / "final-handoff.validation.json"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(FINALIZER),
            "render",
            "--handoff",
            str(handoff_path),
            "--out-final",
            str(final_path),
            "--out-validation",
            str(validation_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "handoff_path": str(handoff_path),
        "handoff_sha256": validation["handoff_sha256"],
        "rendered_path": str(final_path),
        "rendered_sha256": validation["rendered_sha256"],
        "validation_path": str(validation_path),
        "branch": "closed",
    }


def _write_closed_artifact(out_dir: Path, code: str = "DUPLICATE") -> Path:
    """Write the smallest successful T0 bundle plus terminal close result."""
    for gate_id in GATE_IDS:
        for suffix in ("command.txt", "stdout.txt", "stderr.txt"):
            (out_dir / f"{gate_id}.{suffix}").write_text("", encoding="utf-8")
    checks = [
        {
            "id": gate_id,
            "status": "not-applicable" if gate_id != "review" else "pass",
            "exit_code": 0,
            "duration_seconds": 0.0,
            "command_path": f"{gate_id}.command.txt",
            "stdout": f"{gate_id}.stdout.txt",
            "stderr": f"{gate_id}.stderr.txt",
            **({"reason": "Detailed source review stopped at the close gate."} if gate_id != "review" else {}),
        }
        for gate_id in GATE_IDS
    ]
    (out_dir / "gates.json").write_text(
        json.dumps({"status": "pass", "checks_failed": [], "checks": checks}), encoding="utf-8"
    )
    (out_dir / "pr.json").write_text(
        json.dumps(
            {
                "number": 123,
                "url": "https://github.com/acme/widgets/pull/123",
                "body": "Replace the already shipped widget implementation.",
                "state": "OPEN",
                "baseRefOid": BASE_OID,
                "headRefOid": HEAD_OID,
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "pr-routing.json").write_text(
        json.dumps({"pr_state": "OPEN", "base_oid": BASE_OID, "head_oid": HEAD_OID}), encoding="utf-8"
    )
    (out_dir / "target-branch.json").write_text(
        json.dumps(
            {
                "status": "fetched",
                "expected_base_oid": BASE_OID,
                "local_head": BASE_OID,
                "expected_base_is_ancestor": True,
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "local-checkout.json").write_text(
        json.dumps(
            {
                "status": "checked-out",
                "expected_head": HEAD_OID,
                "local_head": HEAD_OID,
                "head_matches_pr": True,
                "diff_source": "verified-local-checkout",
                "diff_base_oid": BASE_OID,
                "diff_head_oid": HEAD_OID,
            }
        ),
        encoding="utf-8",
    )
    for filename, payload in (
        ("remote-selection.json", {}),
        ("comments.json", []),
        ("reviews.json", []),
        ("review-threads.json", []),
        ("unresolved-review-threads.json", []),
        ("online-review-summary.json", {}),
    ):
        (out_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    (out_dir / "diff.patch").write_text("diff --git a/widget.py b/widget.py\n", encoding="utf-8")
    (out_dir / "files.txt").write_text("widget.py\n", encoding="utf-8")
    (out_dir / "untracked.txt").write_text("", encoding="utf-8")
    (out_dir / "numstat.txt").write_text("1\t1\twidget.py\n", encoding="utf-8")

    decision = {
        "schema_version": 1,
        "code": code,
        "advisory_only": True,
        "head_sha": HEAD_OID,
        "summary": "The proposed outcome is already present upstream.",
        "rationale": "A merged change with the same verified goal makes this PR redundant.",
        "evidence": [
            {
                "claim": "The equivalent change is already merged.",
                "source": "https://github.com/acme/widgets/pull/100",
            },
            {
                "claim": "The current PR states the same outcome.",
                "source": "pr.json#body",
            },
        ],
        "counterevidence_checked": ["Verified that the prior change remains present on the refreshed target branch."],
    }
    (out_dir / "review-notes.md").write_text(
        "# Review Decision: close\n\n"
        "Source findings: not assessed\n\n"
        "Detailed review: skipped\n\n"
        f"Close reason: `{code}`\n\n"
        f"Summary: {decision['summary']}\n\n"
        f"Rationale: {decision['rationale']}\n\n"
        "Evidence:\n\n"
        f"- `{decision['evidence'][0]['source']}`: {decision['evidence'][0]['claim']}\n"
        f"- `{decision['evidence'][1]['source']}`: {decision['evidence'][1]['claim']}\n\n"
        "Counterevidence checked:\n\n"
        f"- {decision['counterevidence_checked'][0]}\n\n"
        "GitHub mutation: not performed.\n",
        encoding="utf-8",
    )
    metadata = {
        "scope": "pr",
        "risk_tier": "TRIVIAL",
        "review_status": "closed",
        "close_decision": decision,
        "confidence_gaps": [CONFIDENCE_GAP],
        "confidence_gap_closures": [
            {
                "gap": CONFIDENCE_GAP,
                "status": "deferred",
                "rationale": "The conclusive proposal-level reason made source findings irrelevant to this decision.",
            }
        ],
        "confidence_recovery": {
            "initial_confidence": 0.9,
            "final_confidence": 0.95,
            "status": "fair",
            "evidence": ["The close reason is bound to the verified PR head and cited evidence."],
            "recovery_actions": ["Cross-checked the close evidence against the PR goal and current head."],
            "remaining_limits": ["Detailed source correctness was not assessed."],
        },
    }
    result_path = out_dir / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "checks_run": list(GATE_IDS),
                "checks_failed": [],
                "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                "confidence": 0.95,
                "artifact_path": str(result_path),
                "metadata": metadata,
            }
        ),
        encoding="utf-8",
    )
    return result_path


@pytest.mark.parametrize("code", CLOSE_CODES)
def test_closed_pr_artifact_accepts_each_close_reason(tmp_path: Path, code: str) -> None:
    """Keep every supported proposal-level close reason machine-validatable."""
    result_path = _write_closed_artifact(tmp_path, code)

    _load_validator()._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        pytest.param("unknown-code", "closed-review-invalid-code", id="unknown-code"),
        pytest.param("local-scope", "closed-review-non-pr-scope", id="local-scope"),
        pytest.param("fail-status", "closed-review-status-must-pass", id="fail-status"),
        pytest.param("normal-decision", "closed-review-unexpected-metadata-fields", id="normal-decision"),
        pytest.param("source-finding", "closed-review-must-not-have-findings", id="source-finding"),
        pytest.param("review-routing", "closed-review-has-detailed-review-artifacts", id="review-routing"),
        pytest.param("finding-table", "closed-review-table-forbidden", id="finding-table"),
        pytest.param("missing-diff", "closed-review-missing-pr-artifact:diff.patch", id="missing-diff"),
        pytest.param("single-source", "closed-review-evidence-required", id="single-source"),
        pytest.param("duplicate-source", "closed-review-distinct-evidence-required", id="duplicate-source"),
        pytest.param("missing-counterevidence", "closed-review-counterevidence-required", id="missing-counterevidence"),
        pytest.param("wrong-head", "closed-review-head-sha-mismatch", id="wrong-head"),
        pytest.param("low-confidence", "closed-review-confidence-below-threshold", id="low-confidence"),
        pytest.param("public-fallback", "closed-review-public-fallback-insufficient", id="public-fallback"),
    ],
)
def test_closed_pr_artifact_rejects_ambiguous_or_detailed_review_state(
    tmp_path: Path, mutation: str, error: str
) -> None:
    """Prevent a terminal close result from masquerading as a completed source review."""
    result_path = _write_closed_artifact(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if mutation == "unknown-code":
        result["metadata"]["close_decision"]["code"] = "CLOSE_DUPLICATE"
    elif mutation == "local-scope":
        result["metadata"]["scope"] = "working-tree"
    elif mutation == "fail-status":
        result["status"] = "fail"
    elif mutation == "normal-decision":
        result["metadata"]["review_decision"] = {
            "recommendation": "reject",
            "summary": "Wrong state.",
            "rationale": "Wrong state.",
        }
    elif mutation == "source-finding":
        result["findings"]["high"] = 1
    elif mutation == "review-routing":
        (tmp_path / "review-routing.json").write_text("{}", encoding="utf-8")
    elif mutation == "finding-table":
        with (tmp_path / "review-notes.md").open("a", encoding="utf-8") as handle:
            handle.write("\n| Finding | Evidence |\n| --- | --- |\n")
    elif mutation == "missing-diff":
        (tmp_path / "diff.patch").unlink()
    elif mutation == "single-source":
        result["metadata"]["close_decision"]["evidence"] = result["metadata"]["close_decision"]["evidence"][:1]
    elif mutation == "duplicate-source":
        result["metadata"]["close_decision"]["evidence"][1]["source"] = result["metadata"]["close_decision"][
            "evidence"
        ][0]["source"]
    elif mutation == "missing-counterevidence":
        result["metadata"]["close_decision"]["counterevidence_checked"] = []
    elif mutation == "wrong-head":
        result["metadata"]["close_decision"]["head_sha"] = "c" * 40
    elif mutation == "low-confidence":
        result["confidence"] = 0.89
        result["metadata"]["confidence_recovery"]["final_confidence"] = 0.89
        result["metadata"]["confidence_recovery"]["status"] = "cautious-low"
    elif mutation == "public-fallback":
        (tmp_path / "online-review-summary.json").write_text(
            json.dumps({"pr_metadata_transport": "public-https-fallback"}), encoding="utf-8"
        )
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(SystemExit, match=error):
        _load_validator()._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)


def test_write_result_closed_review_omits_remediation_fields(tmp_path: Path) -> None:
    """Keep a terminal close result out of the normal recommendation/follow-up shape."""
    result_path = _write_closed_artifact(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    candidate_path = tmp_path / "result.candidate.json"
    result["metadata"]["final_handoff"] = _write_closed_final_handoff(tmp_path, result_path, result["metadata"])

    completed = subprocess.run(
        [
            sys.executable,
            str(WRITE_RESULT),
            "--out",
            str(candidate_path),
            "--gates",
            str(tmp_path / "gates.json"),
            "--status",
            "pass",
            "--checks-run",
            ",".join(GATE_IDS),
            "--confidence",
            "0.95",
            "--artifact-path",
            str(result_path),
            "--metadata",
            json.dumps(result["metadata"]),
            "--recommendations",
            "must-not-survive",
            "--follow-up",
            "must-not-survive",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert "recommendations" not in candidate
    assert "follow_up" not in candidate

    _load_validator()._validate_result(tmp_path, candidate_path, tmp_path, "thread", tmp_path)
    _load_shared_validator().validate("code-review", tmp_path, candidate_path)


def test_close_gate_precedes_detailed_review_and_documents_blocking_defaults() -> None:
    """Keep the early terminal path and normal blocking policy visible in the skill contract."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")

    close_gate = skill.index("Terminal close gate")
    assert close_gate < skill.index("Structural context (optional)")
    assert close_gate < skill.index("### 03: T1 primary diff review")
    assert "If evidence is inconclusive, continue to T1/T2" in skill
    assert "Missing CHANGELOG entry alone" in skill
    assert "Merge conflicts" in skill and "not blocking" in skill
    assert "Missing CLA/DCO" in skill and "only when the project requires it" in skill
    for code in CLOSE_CODES:
        assert f"`{code}`" in skill
