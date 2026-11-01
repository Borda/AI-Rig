"""Terminal PR close decisions: result validation, result writing, and the skill's close-gate contract text."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from _artifact_files import write_json
from _modules import load_module
from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
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


def _write_closed_final_handoff(out_dir: Path, result_path: Path, metadata: dict[str, object]) -> dict[str, object]:
    """Render the terminal close response with the real finalizer and return its result metadata binding."""
    gates = json.loads((out_dir / "gates.json").read_text(encoding="utf-8"))
    recovery = metadata["confidence_recovery"]
    assert isinstance(recovery, dict)
    closures = metadata["confidence_gap_closures"]
    assert isinstance(closures, list)
    handoff = {
        "schema_version": 1,
        "presentation_version": 8,
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
        "confidence": {"score": 0.95, "band": "fair", "limits": recovery["remaining_limits"], "gaps": closures},
        "artifacts": [{"label": "Result", "path": str(result_path)}],
        "caller_contract": None,
    }
    handoff_path = out_dir / "final-handoff.json"
    final_path = out_dir / "final.md"
    validation_path = out_dir / "final-handoff.validation.json"
    write_json(handoff_path, handoff)
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
    write_json(out_dir / "gates.json", {"status": "pass", "checks_failed": [], "checks": checks})
    write_json(
        out_dir / "pr.json",
        {
            "number": 123,
            "url": "https://github.com/acme/widgets/pull/123",
            "body": "Replace the already shipped widget implementation.",
            "state": "OPEN",
            "baseRefOid": BASE_OID,
            "headRefOid": HEAD_OID,
        },
    )
    write_json(out_dir / "pr-routing.json", {"pr_state": "OPEN", "base_oid": BASE_OID, "head_oid": HEAD_OID})
    write_json(
        out_dir / "target-branch.json",
        {
            "status": "fetched",
            "remote_ref": BASE_OID,
            "expected_base_oid": BASE_OID,
            "local_head": BASE_OID,
            "expected_base_is_ancestor": True,
        },
    )
    write_json(
        out_dir / "pr-head-fetch.json",
        {
            "status": "fetched",
            "remote_ref": "FETCH_HEAD",
            "expected_head_oid": HEAD_OID,
            "local_head": HEAD_OID,
            "head_matches_pr_metadata": True,
        },
    )
    write_json(
        out_dir / "local-checkout.json",
        {
            "status": "checked-out",
            "expected_head": HEAD_OID,
            "local_head": HEAD_OID,
            "head_matches_pr": True,
            "diff_source": "verified-local-checkout",
            "diff_base_oid": BASE_OID,
            "diff_head_oid": HEAD_OID,
        },
    )
    write_json(
        out_dir / "worktree-preflight.json",
        {
            "phase": "after-checkout",
            "status": "clean",
            "current_head": HEAD_OID,
            "expected_head": HEAD_OID,
            "dirty_paths": [],
            "unmerged_paths": [],
            "pr_paths": ["widget.py"],
            "checkout_paths": [],
            "overlapping_paths": [],
            "overlapping_pr_paths": [],
        },
    )
    for filename, payload in (
        ("remote-selection.json", {}),
        ("comments.json", []),
        ("reviews.json", []),
        ("review-threads.json", []),
        ("unresolved-review-threads.json", []),
        ("online-review-summary.json", {}),
    ):
        write_json(out_dir / filename, payload)
    (out_dir / "diff.patch").write_text("diff --git a/widget.py b/widget.py\n", encoding="utf-8", newline="\n")
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
    write_json(
        result_path,
        {
            "status": "pass",
            "checks_run": list(GATE_IDS),
            "checks_failed": [],
            "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "confidence": 0.95,
            "artifact_path": str(result_path),
            "metadata": metadata,
        },
    )
    return result_path


def _decision(result: dict) -> dict:
    """Return the close decision inside a result dict."""
    return result["metadata"]["close_decision"]


def _unknown_code(result: dict, out_dir: Path) -> None:
    """Replace the close code with one outside the supported set."""
    _decision(result)["code"] = "CLOSE_DUPLICATE"


def _local_scope(result: dict, out_dir: Path) -> None:
    """Claim a local working-tree scope for a PR close."""
    result["metadata"]["scope"] = "working-tree"


def _fail_status(result: dict, out_dir: Path) -> None:
    """Mark the terminal close result as failed."""
    result["status"] = "fail"


def _normal_decision(result: dict, out_dir: Path) -> None:
    """Add a normal review recommendation next to the close decision."""
    result["metadata"]["review_decision"] = {
        "recommendation": "reject",
        "summary": "Wrong state.",
        "rationale": "Wrong state.",
    }


def _source_finding(result: dict, out_dir: Path) -> None:
    """Report a source finding on a close result."""
    result["findings"]["high"] = 1


def _review_routing(result: dict, out_dir: Path) -> None:
    """Leave a detailed-review routing artifact beside the close result."""
    (out_dir / "review-routing.json").write_text("{}", encoding="utf-8")


def _finding_table(result: dict, out_dir: Path) -> None:
    """Append a findings table to the close notes."""
    with (out_dir / "review-notes.md").open("a", encoding="utf-8") as handle:
        handle.write("\n| Finding | Evidence |\n| --- | --- |\n")


def _missing_diff(result: dict, out_dir: Path) -> None:
    """Remove the PR diff artifact."""
    (out_dir / "diff.patch").unlink()


def _single_source(result: dict, out_dir: Path) -> None:
    """Keep only one evidence source."""
    _decision(result)["evidence"] = _decision(result)["evidence"][:1]


def _duplicate_source(result: dict, out_dir: Path) -> None:
    """Make both evidence entries cite the same source."""
    evidence = _decision(result)["evidence"]
    evidence[1]["source"] = evidence[0]["source"]


def _missing_counterevidence(result: dict, out_dir: Path) -> None:
    """Drop the checked counterevidence."""
    _decision(result)["counterevidence_checked"] = []


def _wrong_head(result: dict, out_dir: Path) -> None:
    """Bind the decision to a head commit other than the PR head."""
    _decision(result)["head_sha"] = "c" * 40


def _low_confidence(result: dict, out_dir: Path) -> None:
    """Lower the close confidence below the accepted threshold."""
    result["confidence"] = 0.89
    result["metadata"]["confidence_recovery"].update(final_confidence=0.89, status="cautious-low")


def _public_fallback(result: dict, out_dir: Path) -> None:
    """Record that PR metadata came from the public HTTPS fallback."""
    write_json(out_dir / "online-review-summary.json", {"pr_metadata_transport": "public-https-fallback"})


@pytest.fixture(scope="module")
def review_validator() -> ModuleType:
    """Load the standalone review validator once from its shipped location."""
    return load_module("code_review_close_validator", REVIEW_VALIDATOR)


@pytest.fixture(scope="module")
def shared_validator() -> ModuleType:
    """Load the shared artifact validator that runs after the review-specific gate, once."""
    return load_module("shared_close_validator", SHARED_VALIDATOR)


@pytest.fixture(scope="module")
def skill() -> str:
    """Return the code-review SKILL.md joined with its source-and-scope reference."""
    return read_with_references(CODE_REVIEW_SKILL, "source-and-scope.md")


@pytest.fixture
def closed_result(tmp_path: Path) -> Path:
    """Write a valid terminal close result (code DUPLICATE) with its full T0 bundle into ``tmp_path``."""
    return _write_closed_artifact(tmp_path)


def _first_line(text: str, needle: str) -> int:
    """Return the index of the first line containing ``needle``."""
    index = next((number for number, line in enumerate(text.splitlines()) if needle in line), None)
    assert index is not None, f"{needle!r} not found"
    return index


def _missing(text: str, *phrases: str) -> list[str]:
    """Return the phrases absent from the whitespace-collapsed ``text``; empty means all are stated."""
    prose = " ".join(text.split())
    return [phrase for phrase in phrases if phrase not in prose]


class TestClosedReviewResult:
    """Validation and writing of the terminal PR close result."""

    @pytest.mark.parametrize("code", CLOSE_CODES)
    def test_accepts_each_close_reason(self, review_validator: ModuleType, tmp_path: Path, code: str):
        """Every supported proposal-level close reason passes machine validation.

        A minimal successful T0 bundle is written for each close code, so a code dropped from the validator's allowed
        set fails here instead of in a real review.
        """
        result_path = _write_closed_artifact(tmp_path, code)

        review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)

    @pytest.mark.parametrize(
        ("mutate", "error"),
        [
            pytest.param(_unknown_code, "closed-review-invalid-code", id="unknown-code"),
            pytest.param(_local_scope, "closed-review-non-pr-scope", id="local-scope"),
            pytest.param(_fail_status, "closed-review-status-must-pass", id="fail-status"),
            pytest.param(_normal_decision, "closed-review-unexpected-metadata-fields", id="normal-decision"),
            pytest.param(_source_finding, "closed-review-must-not-have-findings", id="source-finding"),
            pytest.param(_review_routing, "closed-review-has-detailed-review-artifacts", id="review-routing"),
            pytest.param(_finding_table, "closed-review-table-forbidden", id="finding-table"),
            pytest.param(_missing_diff, "closed-review-missing-pr-artifact:diff.patch", id="missing-diff"),
            pytest.param(_single_source, "closed-review-evidence-required", id="single-source"),
            pytest.param(_duplicate_source, "closed-review-distinct-evidence-required", id="duplicate-source"),
            pytest.param(
                _missing_counterevidence, "closed-review-counterevidence-required", id="missing-counterevidence"
            ),
            pytest.param(_wrong_head, "closed-review-head-sha-mismatch", id="wrong-head"),
            pytest.param(_low_confidence, "closed-review-confidence-below-threshold", id="low-confidence"),
            pytest.param(_public_fallback, "closed-review-public-fallback-insufficient", id="public-fallback"),
        ],
    )
    def test_rejects_ambiguous_or_detailed_review_state(
        self,
        review_validator: ModuleType,
        closed_result: Path,
        mutate: Callable[[dict, Path], None],
        error: str,
    ):
        """A terminal close result cannot masquerade as a completed source review.

        Each mutator breaks one close invariant (code, scope, status, findings, evidence, head binding, confidence or
        transport) on an otherwise valid close result and the validator must name that exact failure.
        """
        out_dir = closed_result.parent
        result = json.loads(closed_result.read_text(encoding="utf-8"))
        mutate(result, out_dir)
        write_json(closed_result, result)

        with pytest.raises(SystemExit, match=error):
            review_validator._validate_result(out_dir, closed_result, out_dir, "thread", out_dir)

    def test_write_result_omits_remediation_fields(
        self, review_validator: ModuleType, shared_validator: ModuleType, closed_result: Path
    ):
        """The real result writer keeps a close result out of the normal recommendation/follow-up shape.

        Runs write-result.py with `--recommendations` and `--follow-up` set on a closed review; the written candidate
        must drop both fields and still pass the review-specific and shared validators.
        """
        out_dir = closed_result.parent
        result = json.loads(closed_result.read_text(encoding="utf-8"))
        candidate_path = out_dir / "result.candidate.json"
        result["metadata"]["final_handoff"] = _write_closed_final_handoff(out_dir, closed_result, result["metadata"])

        completed = subprocess.run(
            [
                sys.executable,
                str(WRITE_RESULT),
                "--out",
                str(candidate_path),
                "--gates",
                str(out_dir / "gates.json"),
                "--status",
                "pass",
                "--checks-run",
                ",".join(GATE_IDS),
                "--confidence",
                "0.95",
                "--artifact-path",
                str(closed_result),
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
        review_validator._validate_result(out_dir, candidate_path, out_dir, "thread", out_dir)
        shared_validator.validate("code-review", out_dir, candidate_path)


class TestCloseGateSkillContract:
    """The code-review skill text that places and defines the terminal close gate."""

    def test_close_gate_precedes_detailed_review(self, skill: str):
        """The terminal close gate is stated before any detailed review step and defers to T1/T2 on weak evidence.

        Reading order is the execution order for the agent, so a close gate placed after the structural context or the
        T1 diff review would let detailed review start before a conclusive close.
        """
        close_gate = _first_line(skill, "Terminal close gate")

        assert close_gate < _first_line(skill, "Structural context (required for Python diffs)")
        assert close_gate < _first_line(skill, "### 03: T1 primary diff review")
        assert not _missing(skill, "If evidence is inconclusive, continue to T1/T2")

    def test_documents_blocking_defaults(self, skill: str):
        """The normal blocking policy for changelog, conflicts and CLA/DCO stays visible in the skill.

        Missing changelog and merge conflicts are not blocking by themselves, and a missing CLA/DCO blocks only when the
        project requires it, so the close gate cannot treat them as grounds to close.
        """
        assert not _missing(
            skill,
            "Missing CHANGELOG entry alone",
            "Merge conflicts",
            "not blocking",
            "Missing CLA/DCO",
            "only when the project requires it",
        )

    def test_names_every_close_code(self, skill: str):
        """Every supported close code is listed in the skill contract.

        The validator accepts the codes in CLOSE_CODES, so a code missing from the skill text could never be chosen by
        the reviewing agent.
        """
        assert not _missing(skill, *(f"`{code}`" for code in CLOSE_CODES))
