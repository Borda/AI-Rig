"""Terminal PR close decisions: result validation, result writing, and the skill's close-gate contract text."""

from __future__ import annotations

import json
import re
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
COMPLETION_REFERENCE = PLUGIN_ROOT / "skills" / "code-review" / "references" / "findings-and-completion.md"
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


def _write_closed_final_handoff(
    out_dir: Path, result_path: Path, metadata: dict[str, object], *, historical: bool = False
) -> dict[str, object]:
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
    if not historical:
        producer = load_module("closed_review_producer", PLUGIN_ROOT / "shared" / "remediation_finalize.py")
        handoff = producer.derive_handoff(out_dir, metadata, handoff, 0.95, str(result_path))
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


def _write_closed_artifact(out_dir: Path, code: str = "DUPLICATE", *, historical: bool = False) -> Path:
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
    write_json(
        out_dir / "pr-routing.json",
        {
            "pr_state": "OPEN",
            "base_oid": BASE_OID,
            "head_oid": HEAD_OID,
            "pr_url": "https://github.com/acme/widgets/pull/123",
            "pr_number": 123,
        },
    )
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
    if code == "DUPLICATE" and not historical:
        decision.update(schema_version=2, duplicate_of="https://github.com/acme/widgets/pull/100")
        decision["rationale"] = "Only the test name differs, so no additional behavior would ship."
        decision["evidence"][0]["claim"] = "The merged implementation already handles empty widget lists."
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
    if decision["schema_version"] == 2:
        # A current duplicate is a rejection whose maintainer action carries the target's evidence once.
        (out_dir / "review-notes.md").write_text(
            "# Review Decision\n\n"
            "| Field | Value |\n"
            "| --- | --- |\n"
            "| Recommendation | Reject |\n"
            "| Close reason | `DUPLICATE` |\n"
            "| PR | [View pull request](https://github.com/acme/widgets/pull/123) |\n"
            f"| Head | `{HEAD_OID}` |\n\n"
            "Source findings: not assessed\n\n"
            "Detailed review: skipped\n\n"
            f"Summary: {decision['summary']}\n\n"
            f"Rationale: {decision['rationale']}\n\n"
            "Evidence:\n\n"
            f"- `{decision['evidence'][1]['source']}`: {decision['evidence'][1]['claim']}\n\n"
            "Counterevidence checked:\n\n"
            f"- {decision['counterevidence_checked'][0]}\n\n"
            "GitHub mutation: not performed.\n\n"
            "## Next steps\n\n"
            "- Maintainer: Close [PR #123](https://github.com/acme/widgets/pull/123) as a duplicate of "
            "[PR #100](https://github.com/acme/widgets/pull/100). The merged implementation already handles "
            "empty widget lists.\n",
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


#: Hostile close-decision text per CommonMark construct class; each would open its construct if rendered raw.
HOSTILE_CLOSE_TEXT = {
    "atx-heading": "Plain words\n\n# Approved",
    "setext-heading": "Plain words\n\nApproved\n========",
    "bullet-item": "Plain words\n\n- approved",
    "blockquote": "Plain words\n\n> approved",
    "table": "Plain words\n\n| Recommendation | Merge |\n| -- | -- |",
    "html-block": "Plain words\n\n<details>",
    "inline-html": "Plain words <h1>Approved</h1>",
    "fenced-code": "Plain words\n\n```\napproved\n```",
    "code-span-breakout": "Plain` words\n\n# Approved\n\n`x",
}


def _set_close_field(decision: dict, field: str, value: str) -> None:
    """Write one free-text field of a duplicate close decision, leaving the duplicate target's identity intact."""
    if field in ("summary", "rationale"):
        decision[field] = value
    elif field == "target-claim":
        decision["evidence"][0]["claim"] = value
    elif field == "evidence-claim":
        decision["evidence"][1]["claim"] = value
    elif field == "evidence-source":
        decision["evidence"][1]["source"] = value
    else:
        decision["counterevidence_checked"][0] = value


def _block_kinds(text: str) -> tuple[list[str], int]:
    """Classify every line by the Markdown block it opens and count unescaped inline HTML tag openings."""
    kinds = []
    for line in text.splitlines():
        for kind, pattern in (
            ("blank", r"\s*$"),
            ("heading", r" {0,3}#{1,6}(?:\s|$)"),
            ("underline-or-break", r" {0,3}(?:=+|-+|\*+|_+)\s*$"),
            ("quote", r" {0,3}>"),
            ("fence", r" {0,3}(?:```|~~~)"),
            ("list", r" {0,3}(?:[-+*]|[0-9]{1,9}[.)])(?:\s|$)"),
            ("table", r" {0,3}\|"),
            ("html", r" {0,3}<[A-Za-z/!?]"),
        ):
            if re.match(pattern, line):
                kinds.append(kind)
                break
        else:
            kinds.append("text")
    return kinds, len(re.findall(r"(?<!\\)<[A-Za-z/!?]", text))


def _closed_bundle(
    out_dir: Path, review_validator: ModuleType, shared_validator: ModuleType, field: str | None, value: str
) -> tuple[str, str]:
    """Finalize, write and validate a duplicate close with one replaced field, returning its notes and final text."""
    result_path = _write_closed_artifact(out_dir)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if field is not None:
        _set_close_field(_decision(result), field, value)
    (out_dir / "review-notes.md").unlink()
    result["metadata"]["final_handoff"] = _write_closed_final_handoff(out_dir, result_path, result["metadata"])
    completed = _write_closed_candidate(out_dir, result["metadata"])
    assert completed.returncode == 0, completed.stderr
    candidate = out_dir / "result.candidate.json"
    review_validator._validate_result(out_dir, candidate, out_dir, "thread", out_dir)
    shared_validator.validate("code-review", out_dir, candidate)
    return (
        (out_dir / "review-notes.md").read_text(encoding="utf-8"),
        (out_dir / "final.md").read_text(encoding="utf-8"),
    )


@pytest.fixture(scope="module")
def canonical_close_structure(
    tmp_path_factory: pytest.TempPathFactory, review_validator: ModuleType, shared_validator: ModuleType
) -> tuple[tuple[list[str], int], tuple[list[str], int]]:
    """Return the block structure of a duplicate close whose every free-text field is ordinary one-line text."""
    notes, final = _closed_bundle(
        tmp_path_factory.mktemp("canonical-close"), review_validator, shared_validator, None, ""
    )
    return _block_kinds(notes), _block_kinds(final)


def _write_closed_candidate(out_dir: Path, metadata: dict) -> subprocess.CompletedProcess[str]:
    """Invoke the standalone writer with the same closed-review inputs used by an operator."""
    return subprocess.run(
        [
            sys.executable,
            str(WRITE_RESULT),
            "--out",
            str(out_dir / "result.candidate.json"),
            "--gates",
            str(out_dir / "gates.json"),
            "--status",
            "pass",
            "--checks-run",
            ",".join(GATE_IDS),
            "--confidence",
            "0.95",
            "--artifact-path",
            str(out_dir / "result.json"),
            "--metadata",
            json.dumps(metadata),
            "--recommendations",
            "must-not-survive",
            "--follow-up",
            "must-not-survive",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


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

    @pytest.mark.parametrize(
        ("target", "source", "error"),
        [
            pytest.param("https://example.org/acme/widgets/pull/1", None, "duplicate-target-invalid", id="external"),
            pytest.param("https://github.com/acme/widgets/pull/AC", None, "duplicate-target-invalid", id="bad-number"),
            pytest.param(
                "https://github.com/acme/widgets/pull/100#comment", None, "duplicate-target-invalid", id="fragment"
            ),
            pytest.param(
                "https://github.com/acme/widgets/commit/abc", None, "duplicate-target-invalid", id="short-sha"
            ),
            pytest.param(
                "https://github.com/Acme/widgets/issues/123", None, "duplicate-target-self", id="issue-redirect"
            ),
            pytest.param("https://github.com/Acme/widgets/pull/123", None, "duplicate-target-self", id="same-pr"),
            pytest.param(
                "https://github.com/acme/widgets/pull/101", "pr.json#title", "duplicate-target-evidence", id="unbound"
            ),
        ],
    )
    def test_requires_canonical_distinct_target_and_matching_evidence(
        self, review_validator: ModuleType, closed_result: Path, target: str, source: str | None, error: str
    ) -> None:
        """Refuse a candidate that could send a maintainer to the wrong change."""
        result = json.loads(closed_result.read_text(encoding="utf-8"))
        decision = _decision(result)
        decision["duplicate_of"] = target
        decision["evidence"][0]["source"] = source or target
        candidate = closed_result.with_name("result.candidate.json")
        write_json(candidate, result)
        with pytest.raises(SystemExit, match=error):
            review_validator._validate_result(candidate.parent, candidate, candidate.parent, "thread", candidate.parent)

    def test_target_url_alone_cannot_prove_the_overlap(self, review_validator: ModuleType, closed_result: Path) -> None:
        """The target evidence must say what the prior work actually ships."""
        result = json.loads(closed_result.read_text(encoding="utf-8"))
        decision = _decision(result)
        decision["evidence"][0]["claim"] = decision["duplicate_of"]
        candidate = closed_result.with_name("result.candidate.json")
        write_json(candidate, result)
        with pytest.raises(SystemExit, match="duplicate-target-evidence-missing"):
            review_validator._validate_result(candidate.parent, candidate, candidate.parent, "thread", candidate.parent)

    @pytest.mark.parametrize(
        ("code", "review_error", "shared_error"),
        [
            pytest.param("DUPLICATE", "duplicate-decision-schema", "duplicate-decision-schema", id="duplicate"),
            pytest.param(
                "FALSE_GOAL", "closed-review-table-forbidden", "closed-review-final-handoff-mismatch", id="rejected"
            ),
        ],
    )
    def test_historical_close_replays_but_cannot_be_new_candidate(
        self,
        tmp_path: Path,
        review_validator: ModuleType,
        shared_validator: ModuleType,
        code: str,
        review_error: str,
        shared_error: str,
    ) -> None:
        """Replay old promoted decisions and reject their historical presentation for new candidates."""
        result_path = _write_closed_artifact(tmp_path, code, historical=True)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["schema_version"] = 3
        result["metadata"]["final_handoff"] = _write_closed_final_handoff(
            tmp_path, result_path, result["metadata"], historical=True
        )
        write_json(result_path, result)
        original = (tmp_path / "final.md").read_bytes()
        review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
        shared_validator.validate("code-review", tmp_path, result_path)
        assert original == (tmp_path / "final.md").read_bytes()
        assert b"| Recommendation | Reject |" not in original
        assert b"| Recommendation | Duplicate |" not in original
        candidate = tmp_path / "result.candidate.json"
        write_json(candidate, result)
        with pytest.raises(SystemExit, match=review_error):
            review_validator._validate_result(tmp_path, candidate, tmp_path, "thread", tmp_path)
        with pytest.raises(SystemExit, match=shared_error):
            shared_validator.validate("code-review", tmp_path, candidate)

    @pytest.mark.parametrize(
        ("target", "label"),
        [
            pytest.param("https://github.com/acme/widgets/pull/100", "PR #100", id="pull"),
            pytest.param("https://github.com/acme/tracker/issues/8", "issue acme/tracker#8", id="issue"),
            pytest.param(
                "https://github.com/acme/core/commit/" + "c" * 40, "commit acme/core@cccccccccccc", id="commit"
            ),
        ],
    )
    def test_finalizer_derives_duplicate_action_from_chosen_reference(
        self, closed_result: Path, target: str, label: str
    ) -> None:
        """Reject a duplicate and link the declared reference even when a different evidence URL appears first.

        The link label names only the target's kind, because the decision carries no evidence of its merge or resolution
        state. The rationale appears once in the rejection and the target evidence once in the action.
        """
        result = json.loads(closed_result.read_text(encoding="utf-8"))
        decision = _decision(result)
        decision["duplicate_of"] = decision["evidence"][0]["source"] = target
        decision["evidence"].insert(
            0, {"claim": "A related proposal remains open.", "source": "https://github.com/acme/widgets/pull/55"}
        )
        (closed_result.parent / "review-notes.md").unlink()
        _write_closed_final_handoff(closed_result.parent, closed_result, result["metadata"])
        handoff = json.loads((closed_result.parent / "final-handoff.json").read_text(encoding="utf-8"))
        assert handoff["presentation_version"] == 17
        assert handoff["outcome"] == {"title": "Review Decision", "summary": "Recommendation: Reject."}
        assert handoff["close_reason"] == {
            "code": "DUPLICATE",
            "summary": decision["summary"],
            "rationale": decision["rationale"],
            "pr_url": "https://github.com/acme/widgets/pull/123",
            "head_sha": HEAD_OID,
        }
        assert handoff["remaining"] == [
            {
                "row_id": "duplicate",
                "owner": "Maintainer",
                "item": "The merged implementation already handles empty widget lists.",
                "next_action": f"Close [PR #123](https://github.com/acme/widgets/pull/123) as a duplicate of [{label}]({target}).",
            }
        ]
        assert handoff["next_steps"] == ["duplicate"]
        notes = (closed_result.parent / "review-notes.md").read_text(encoding="utf-8")
        assert "| Recommendation | Reject |" in notes.splitlines()
        assert "| Close reason | `DUPLICATE` |" in notes.splitlines()
        assert f"[{label}]({target})" in notes.split("## Next steps")[1]
        final = (closed_result.parent / "final.md").read_text(encoding="utf-8")
        assert "| Recommendation | Reject |" in final.splitlines()
        assert "| Reason | DUPLICATE |" in final.splitlines()
        assert f"| Reviewed commit | `{HEAD_OID}` |" in final.splitlines()
        assert f"[{label}]({target})" in final.split("**Next steps**")[1]
        for text in (notes, final):
            assert text.count(decision["rationale"]) == 1
            assert text.count("The merged implementation already handles empty widget lists.") == 1

    @pytest.mark.parametrize("code", CLOSE_CODES)
    def test_close_derives_reject_reason_and_preserves_verified_source(
        self, tmp_path: Path, code: str, review_validator: ModuleType, shared_validator: ModuleType
    ) -> None:
        """Every close, duplicates included, names the rejection, reason, reviewed PR and head in the final bundle."""
        result_path = _write_closed_artifact(tmp_path, code)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        decision = _decision(result)
        result["metadata"]["final_handoff"] = _write_closed_final_handoff(tmp_path, result_path, result["metadata"])
        completed = _write_closed_candidate(tmp_path, result["metadata"])
        assert completed.returncode == 0, completed.stderr
        candidate = tmp_path / "result.candidate.json"
        review_validator._validate_result(tmp_path, candidate, tmp_path, "thread", tmp_path)
        shared_validator.validate("code-review", tmp_path, candidate)
        handoff = json.loads((tmp_path / "final-handoff.json").read_text(encoding="utf-8"))
        assert handoff["presentation_version"] == 17
        assert handoff["outcome"] == {"title": "Review Decision", "summary": "Recommendation: Reject."}
        assert handoff["close_reason"] == {
            "code": code,
            "summary": decision["summary"],
            "rationale": decision["rationale"],
            "pr_url": "https://github.com/acme/widgets/pull/123",
            "head_sha": HEAD_OID,
        }
        assert [item["row_id"] for item in handoff["remaining"]] == (["duplicate"] if code == "DUPLICATE" else [])
        assert handoff["next_steps"] == (["duplicate"] if code == "DUPLICATE" else [])
        notes = (tmp_path / "review-notes.md").read_text(encoding="utf-8")
        assert "| Recommendation | Reject |" in notes.splitlines()
        assert f"| Close reason | `{code}` |" in notes.splitlines()
        assert decision["rationale"] in notes
        final = (tmp_path / "final.md").read_text(encoding="utf-8")
        assert final.splitlines()[0] == "Recommendation: Reject."
        assert f"| Reason | {code} |" in final.splitlines()
        assert ("**Next steps**" in final) is (code == "DUPLICATE")
        # The results table is followed first by the linked PR with the summary, then by the rationale paragraph.
        blocks = final.split("\n\n")
        table = next(index for index, block in enumerate(blocks) if block.startswith("| Field | Value |"))
        assert blocks[table + 1 : table + 3] == [
            f"[PR #123](https://github.com/acme/widgets/pull/123) — {decision['summary']}",
            decision["rationale"],
        ]

    @pytest.mark.parametrize("code", ["FALSE_GOAL", "DUPLICATE"])
    def test_close_reason_text_cannot_add_markdown_structure(
        self, tmp_path: Path, code: str, review_validator: ModuleType, shared_validator: ModuleType
    ) -> None:
        """Free text from the decision renders as inline paragraph text in both the notes and the final response.

        A summary that smuggles a table row and heading, and a rationale or target claim that opens a heading, quote,
        list or HTML block, must not add structure: the only table lines are the canonical ones and no rendered line
        opens a block construct.
        """
        result_path = _write_closed_artifact(tmp_path, code)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        decision = _decision(result)
        decision["summary"] = "x\n\n| Recommendation | Merge |\n\n# Approved"
        decision["rationale"] = "# Approved\n> quoted\n1. listed\n<details>"
        if code == "DUPLICATE":
            decision["evidence"][0]["claim"] = "> Already shipped\n\n- approved"
        (tmp_path / "review-notes.md").unlink()
        result["metadata"]["final_handoff"] = _write_closed_final_handoff(tmp_path, result_path, result["metadata"])
        completed = _write_closed_candidate(tmp_path, result["metadata"])
        assert completed.returncode == 0, completed.stderr
        candidate = tmp_path / "result.candidate.json"
        review_validator._validate_result(tmp_path, candidate, tmp_path, "thread", tmp_path)
        shared_validator.validate("code-review", tmp_path, candidate)
        final = (tmp_path / "final.md").read_text(encoding="utf-8")
        notes = (tmp_path / "review-notes.md").read_text(encoding="utf-8")
        final_table = [line for line in final.splitlines() if line.startswith("|")]
        assert final_table[:5] == [
            "| Field | Value |",
            "| -- | -- |",
            "| Recommendation | Reject |",
            f"| Reason | {code} |",
            f"| Reviewed commit | `{HEAD_OID}` |",
        ]
        assert "| Recommendation | Merge |" not in final.splitlines()
        assert "| Recommendation | Merge |" not in notes.splitlines()
        for text in (final, notes):
            opened = [
                line
                for line in text.splitlines()
                if re.match(r"(?:#+ Approved|>|- approved|1\. listed|<details)", line)
            ]
            assert opened == []

    @pytest.mark.parametrize("construct", HOSTILE_CLOSE_TEXT)
    @pytest.mark.parametrize(
        "field", ["summary", "rationale", "target-claim", "evidence-claim", "evidence-source", "counterevidence"]
    )
    def test_close_free_text_keeps_the_canonical_block_structure(
        self,
        tmp_path: Path,
        review_validator: ModuleType,
        shared_validator: ModuleType,
        canonical_close_structure: tuple[tuple[list[str], int], tuple[list[str], int]],
        field: str,
        construct: str,
    ) -> None:
        """No free-text close field can add a heading, list, quote, table, fence or HTML to the notes or final response.

        Both validators accept the bundle, and its block structure line by line equals the bundle whose fields are
        ordinary one-line text.
        """
        notes, final = _closed_bundle(
            tmp_path, review_validator, shared_validator, field, HOSTILE_CLOSE_TEXT[construct]
        )
        assert (_block_kinds(notes), _block_kinds(final)) == canonical_close_structure

    def test_validator_rejects_notes_that_print_a_source_as_markdown(
        self, tmp_path: Path, review_validator: ModuleType, shared_validator: ModuleType
    ) -> None:
        """Hand-written notes that print an evidence source raw, so that it opens a heading, fail validation."""
        raw = "pr.json#body\n\n# Approved"
        notes, _ = _closed_bundle(tmp_path, review_validator, shared_validator, "evidence-source", raw)
        finalizer = load_module("close_source_display", FINALIZER)
        shown = finalizer.literal_code_span(finalizer.inline_text(raw))
        raw_notes = notes.replace(shown, f"`{raw}`")
        assert raw_notes != notes
        (tmp_path / "review-notes.md").write_text(raw_notes, encoding="utf-8")
        with pytest.raises(SystemExit, match="closed-review-notes-must-be-close-only"):
            review_validator._validate_result(
                tmp_path, tmp_path / "result.candidate.json", tmp_path, "thread", tmp_path
            )

    @pytest.mark.parametrize(
        ("field", "reason"),
        [
            pytest.param("evidence-source", "closed-review-empty-evidence", id="source"),
            pytest.param("evidence-claim", "closed-review-empty-evidence", id="claim"),
            pytest.param("counterevidence", "closed-review-counterevidence-required", id="counterevidence"),
        ],
    )
    def test_close_text_made_only_of_invisible_characters_is_refused(
        self, tmp_path: Path, field: str, reason: str
    ) -> None:
        """A value that displays as nothing cannot stand in for the required evidence or counterevidence."""
        result_path = _write_closed_artifact(tmp_path)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        _set_close_field(_decision(result), field, "\u200b\u2060")
        (tmp_path / "review-notes.md").unlink()
        with pytest.raises(SystemExit, match=reason):
            _write_closed_final_handoff(tmp_path, result_path, result["metadata"])

    @pytest.mark.parametrize("code", ["FALSE_GOAL", "DUPLICATE"])
    def test_close_states_terminal_facts_and_the_validator_requires_them(
        self, tmp_path: Path, code: str, review_validator: ModuleType, shared_validator: ModuleType
    ) -> None:
        """Every close states that findings were not assessed, review was skipped and GitHub was not changed.

        The facts are part of the rendered response, so a final response missing one fails the shared validator.
        """
        result_path = _write_closed_artifact(tmp_path, code)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["metadata"]["final_handoff"] = _write_closed_final_handoff(tmp_path, result_path, result["metadata"])
        completed = _write_closed_candidate(tmp_path, result["metadata"])
        assert completed.returncode == 0, completed.stderr
        candidate = tmp_path / "result.candidate.json"
        shared_validator.validate("code-review", tmp_path, candidate)
        final_path = tmp_path / "final.md"
        final = final_path.read_text(encoding="utf-8")
        assert [line for line in final.split("\n\n**Verification**")[0].splitlines() if line.startswith("|")] == [
            "| Field | Value |",
            "| -- | -- |",
            "| Recommendation | Reject |",
            f"| Reason | {code} |",
            f"| Reviewed commit | `{HEAD_OID}` |",
            "| Source findings | Not assessed |",
            "| Detailed review | Skipped |",
            "| GitHub mutation | Not performed |",
        ]
        final_path.write_text(final.replace("| Detailed review | Skipped |\n", ""), encoding="utf-8")
        with pytest.raises(SystemExit, match="rendered-final-mismatch"):
            shared_validator.validate("code-review", tmp_path, candidate)

    def test_close_evidence_location_matches_the_completion_reference(
        self, tmp_path: Path, review_validator: ModuleType, shared_validator: ModuleType
    ) -> None:
        """The reference and the rendered close agree on where evidence and terminal facts appear.

        A duplicate's evidence citing its target is the overlap shown in the maintainer action of the final response;
        every other evidence entry and the counterevidence stay in the notes only.
        """
        notes, final = _closed_bundle(tmp_path, review_validator, shared_validator, None, "")
        decision = _decision(json.loads((tmp_path / "result.json").read_text(encoding="utf-8")))
        assert decision["evidence"][0]["source"] == decision["duplicate_of"]
        assert decision["evidence"][0]["claim"] in final.split("**Next steps**")[1]
        for text in (decision["evidence"][1]["claim"], decision["counterevidence_checked"][0]):
            assert text in notes
            assert text not in final
        lines = COMPLETION_REFERENCE.read_text(encoding="utf-8").splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("For every terminal close code"))
        bullets = []
        for line in lines[start + 1 :]:
            if line.startswith("- "):
                bullets.append(line)
            elif bullets and line.strip():
                break
        evidence = next(line for line in bullets if "`review-notes.md`" in line and "decisive evidence" in line)
        assert all(phrase in evidence for phrase in ("`DUPLICATE`", "maintainer action", "final response"))
        stated = next(line for line in bullets if "`Not assessed`" in line)
        assert all(value in stated for value in ("`Skipped`", "`Not performed`"))
        for row in (
            "| Source findings | Not assessed |",
            "| Detailed review | Skipped |",
            "| GitHub mutation | Not performed |",
        ):
            assert row in final.splitlines()

    def test_duplicate_requires_evidence_beyond_its_target(self, closed_result: Path) -> None:
        """A duplicate whose every evidence entry cites the target fails before any notes are written."""
        result = json.loads(closed_result.read_text(encoding="utf-8"))
        decision = _decision(result)
        decision["evidence"][1]["source"] = decision["duplicate_of"]
        notes = closed_result.parent / "review-notes.md"
        notes.unlink()
        producer = load_module("closed_review_evidence_producer", PLUGIN_ROOT / "shared" / "remediation_finalize.py")
        draft = {"skill": "code-review", "branch": "closed", "artifacts": [], "remaining": [], "next_steps": []}
        with pytest.raises(SystemExit, match="closed-review-distinct-evidence-required"):
            producer.derive_handoff(closed_result.parent, result["metadata"], draft, 0.95, str(closed_result))
        assert not notes.exists()

    def test_pre_upgrade_duplicate_decision_names_its_repair(self, tmp_path: Path) -> None:
        """A resumed schema-1 duplicate fails closed and says which fields select the earlier change."""
        result_path = _write_closed_artifact(tmp_path, "DUPLICATE", historical=True)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        producer = load_module("closed_review_upgrade_producer", PLUGIN_ROOT / "shared" / "remediation_finalize.py")
        draft = {"skill": "code-review", "branch": "closed", "artifacts": [], "remaining": [], "next_steps": []}
        with pytest.raises(SystemExit) as failure:
            producer.derive_handoff(tmp_path, result["metadata"], draft, 0.95, str(result_path))
        message = str(failure.value)
        assert message.startswith("duplicate-decision-schema-v2-required:")
        assert "schema_version" in message
        assert "duplicate_of" in message

    def test_writer_and_validator_reject_action_from_another_decision(
        self, review_validator: ModuleType, shared_validator: ModuleType, closed_result: Path
    ) -> None:
        """A rendered and digest-bound advisory cannot lend its evidence to another duplicate decision."""
        run = closed_result.parent
        result = json.loads(closed_result.read_text(encoding="utf-8"))
        result["schema_version"] = 3
        result["metadata"]["final_handoff"] = _write_closed_final_handoff(run, closed_result, result["metadata"])
        prior_rationale = _decision(result)["rationale"]
        _decision(result)["rationale"] = "The unmerged feature does not address the same condition."
        notes = run / "review-notes.md"
        notes.write_text(
            notes.read_text(encoding="utf-8").replace(prior_rationale, _decision(result)["rationale"]), encoding="utf-8"
        )
        completed = _write_closed_candidate(run, result["metadata"])
        assert completed.returncode != 0
        assert "closed-review-final-handoff-mismatch" in completed.stderr
        candidate = run / "result.candidate.json"
        assert not candidate.exists()
        write_json(candidate, result)
        with pytest.raises(SystemExit, match="closed-review-final-handoff-mismatch"):
            review_validator._validate_result(run, candidate, run, "thread", run)
        with pytest.raises(SystemExit, match="closed-review-final-handoff-mismatch"):
            shared_validator.validate("code-review", run, candidate)

    @pytest.mark.parametrize(
        ("existing", "fallback", "expected_failure"),
        [
            pytest.param("old", False, None, id="upgrade-canonical-notes"),
            pytest.param("old", True, "closed-review-public-fallback-insufficient", id="restore-on-failure"),
            pytest.param("unrelated", False, "closed-review-notes-conflict", id="preserve-authored-notes"),
        ],
    )
    def test_finalization_preserves_notes_and_promotes_only_verified_decision(
        self, tmp_path: Path, existing: str, fallback: bool, expected_failure: str | None
    ) -> None:
        """Exercise the finalizer CLI with a canonical migration, failed validation and unrelated authored notes."""
        historic = _write_closed_artifact(tmp_path, historical=True)
        result = json.loads(historic.read_text(encoding="utf-8"))
        decision = _decision(result)
        decision.update(schema_version=2, duplicate_of=decision["evidence"][0]["source"])
        notes = tmp_path / "review-notes.md"
        prior = notes.read_bytes() if existing == "old" else b"# Authored review notes\r\nAn unrelated judgment.\r\n"
        notes.write_bytes(prior)
        write_json(
            tmp_path / "online-review-summary.json",
            {"pr_metadata_transport": "public-https-fallback" if fallback else "gh"},
        )
        write_json(tmp_path / "metadata.json", result["metadata"])
        write_json(
            tmp_path / "draft.json",
            {
                "branch": "closed",
                "tables": [],
                "source_records": [],
                "source_coverage": {
                    "source_records_total": 0,
                    "represented_source_records_total": 0,
                    "omitted_source_records_total": 0,
                },
            },
        )
        original_result = historic.read_bytes()
        completed = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "shared" / "remediation_finalize.py"),
                "finalize",
                "--skill",
                "code-review",
                "--run",
                str(tmp_path),
                "--metadata",
                str(tmp_path / "metadata.json"),
                "--handoff",
                str(tmp_path / "draft.json"),
                "--status",
                "pass",
                "--confidence",
                "0.95",
                "--artifact-path",
                str(historic),
                "--parent-thread-id",
                "thread",
                "--promote",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        summary = json.loads(completed.stdout)
        outcome = summary["status"]
        assert outcome == ("fail" if expected_failure else "pass"), summary
        assert summary["promoted"] is (expected_failure is None)
        assert (expected_failure or "| Recommendation | Reject |") in (
            completed.stdout if expected_failure else notes.read_text()
        )
        assert (notes.read_bytes() == prior) is bool(expected_failure)
        assert (historic.read_bytes() == original_result) is bool(expected_failure)

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

        completed = _write_closed_candidate(out_dir, result["metadata"])

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
