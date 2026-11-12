"""Bind an unexecuted test gate to a retained user decision.

## Purpose

Share the test execution acceptance rule between the gate runner, result writer, and artifact validator, so an
unexecuted test gate is accepted only when a retained user decision covers it. One narrow exception exists: a Code
Remediate run whose changes are documentation only records the tests gate as not-applicable with the exact reason
"documentation-only change", as the shared Sandboxed Test Runs contract defines, and needs no user decision because
nothing executable changed.

## Scope

Read a small regular approval file inside the run directory, verify its digest, and compare its decision and reason with
the test gate. The helper never executes commands or infers approval from a model's explanation. The documentation-only
exception matches only the exact reason string for the Code Remediate skill; it cannot inspect the changed paths, so the
documentation-only classification itself stays an instruction-level obligation of that workflow.

## Usage

The runner retains a supplied decision with retain_approval; result consumers call validate_pass for executed gate
evidence. The skill-aware validator requires tests for implementation and review for analytical workflows.

## Outputs

The runner receives a relative path and SHA-256 binding suitable for the tests gate's approval field.

## Failure

Missing tests, malformed decisions, changed bytes, symlinks, and paths outside the run fail with coded errors.

## Used by

shared/run_gates.py, shared/write-result.py, and shared/validate-artifacts.py at their acceptance gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

REVIEW_EVIDENCE_SKILLS = frozenset(
    {
        "assess",
        "change-analysis",
        "audit",
        "calibrate",
        "code-review",
        "investigate",
        "challenge-resolve",
        "research",
        "profile",
        "release",
        "sync",
        "manage",
    }
)
#: Exact tests-gate skip reason for a change whose every path is user documentation.
DOCUMENTATION_ONLY_SKIP_REASON = "documentation-only change"
#: Implementation workflows whose contract admits the documentation-only skip without a user decision.
DOCUMENTATION_ONLY_SKIP_SKILLS = frozenset({"code-remediate"})


def validate_pass(gates: dict[str, Any], out_dir: Path | None = None, *, skill: str | None = None) -> None:
    """Require executed evidence, applying the mandatory gate only when the consumer knows the skill.

    The generic result writer has no skill identity; the artifact validator enforces that identity's contract before
    promotion. Any supplied skip approval remains integrity-checked even for a workflow that does not require tests.
    """
    review_required = skill in REVIEW_EVIDENCE_SKILLS
    if review_required and not any(check["id"] == "review" and check["status"] == "pass" for check in gates["checks"]):
        raise SystemExit(f"{skill}-review-gate-required")
    if not any(check["status"] == "pass" for check in gates["checks"]):
        raise SystemExit("result-pass-without-executed-gates")
    approval_present = any(check["id"] == "tests" and "approval" in check for check in gates["checks"])
    if (skill is not None and not review_required) or approval_present:
        validate_tests(gates, {}, out_dir, documentation_only_allowed=skill in DOCUMENTATION_ONLY_SKIP_SKILLS)


def _read_decision(path: Path) -> bytes:
    """Read a bounded regular decision without blocking on a substituted FIFO."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if path.is_symlink():
            raise ValueError("symlink")
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not regular")
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized")
        return raw
    except (OSError, ValueError) as error:
        raise SystemExit("test-skip-approval-unreadable") from error


def _check_decision(raw: bytes, reason: object) -> None:
    """Require an explicit skip decision with the gate's exact reason and user evidence."""
    try:
        decision = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise SystemExit("test-skip-approval-invalid") from error
    if (
        not isinstance(decision, dict)
        or decision.get("decision") != "skip-tests"
        or not isinstance(reason, str)
        or not reason.strip()
        or decision.get("reason") != reason
        or not isinstance(decision.get("evidence"), str)
        or not decision["evidence"].strip()
    ):
        raise SystemExit("test-skip-approval-invalid")


def retain_approval(path: Path, out_dir: Path, reason: str) -> dict[str, str]:
    """Retain the supplied user decision as a digest-bound gate artifact."""
    raw = _read_decision(path)
    _check_decision(raw, reason)
    target = out_dir / "checks/tests-skip-approval.json"
    if target.is_symlink() or not target.resolve().is_relative_to(out_dir.resolve()):
        raise SystemExit("test-skip-approval-invalid-path")
    target.write_bytes(raw)
    return {"path": "checks/tests-skip-approval.json", "sha256": hashlib.sha256(raw).hexdigest()}


def validate_tests(
    gates: dict[str, Any],
    metadata: dict[str, Any],
    out_dir: Path | None = None,
    *,
    closed_review: bool = False,
    documentation_only_allowed: bool = False,
) -> None:
    """Require executed tests or an intact user-approved skip for passing implementation results.

    When ``documentation_only_allowed`` is set, an unapproved not-applicable tests gate whose reason is exactly
    ``DOCUMENTATION_ONLY_SKIP_REASON`` is also accepted. A supplied approval is still integrity-checked.
    """
    test = next((check for check in gates["checks"] if check.get("id") == "tests"), {})
    if test.get("status") == "pass" or (closed_review and metadata.get("review_status") == "closed"):
        return
    if (
        documentation_only_allowed
        and "approval" not in test
        and test.get("status") == "not-applicable"
        and test.get("reason") == DOCUMENTATION_ONLY_SKIP_REASON
    ):
        return
    binding = test.get("approval")
    if test.get("status") != "not-applicable" or not isinstance(binding, dict):
        raise SystemExit("tests-pass-or-approved-skip-required")
    if out_dir is None:
        return
    relative = binding.get("path")
    if relative != "checks/tests-skip-approval.json":
        raise SystemExit("test-skip-approval-invalid-path")
    path = out_dir / relative
    if not path.resolve().is_relative_to(out_dir.resolve()):
        raise SystemExit("test-skip-approval-invalid-path")
    raw = _read_decision(path)
    if hashlib.sha256(raw).hexdigest() != binding.get("sha256"):
        raise SystemExit("test-skip-approval-digest-mismatch")
    _check_decision(raw, test.get("reason"))
