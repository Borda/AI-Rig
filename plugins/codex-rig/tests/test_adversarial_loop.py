"""Regression checks for the deterministic adversarial-review convergence ledger."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Any

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = PLUGIN_ROOT / "shared" / "adversarial_loop.py"


def _load_module() -> ModuleType:
    """Load the standalone convergence-ledger helper without installation."""
    specification = importlib.util.spec_from_file_location("codex_rig_adversarial_loop", LEDGER_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_public_validator_imports() -> None:
    """Expose the validator required by skill lifecycle owners."""
    module = _load_module()

    assert callable(module.validate_ledger)


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _finding(
    signature: str,
    tier: str = "high",
    disposition: str = "open",
    structural: bool = False,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    """Build a recorded finding with explicit disposition and evidence."""
    if evidence is None:
        evidence = ["review evidence"] if disposition in {"rejected", "verified-fixed"} else []
    return {
        "signature": signature,
        "tier": tier,
        "structural": structural,
        "disposition": disposition,
        "evidence": evidence,
    }


def _round(
    index: int, findings: list[dict[str, Any]], digest: str = _DIGEST_A, independent: bool = True
) -> dict[str, Any]:
    """Build one declared independent review of one snapshot."""
    return {
        "index": index,
        "reviewer": {"identity": "independent-reviewer", "independent": independent},
        "snapshot": {"revision": "HEAD", "diff_digest": digest},
        "report_path": f"reports/review-{index}.json",
        "findings": findings,
    }


def _ledger(*rounds: dict[str, Any], digest: str = _DIGEST_A) -> dict[str, Any]:
    """Build a valid convergence ledger whose current snapshot is configurable."""
    return {
        "schema_version": 1,
        "implementation_author": "implementation-author",
        "current_snapshot": {"revision": "HEAD", "diff_digest": digest},
        "rounds": list(rounds),
    }


def test_initial_review_is_baseline_and_pending_counts_open() -> None:
    """Keep the initial W_0 and pending-verification weight in the score."""
    module = _load_module()
    ledger = _ledger(_round(1, [_finding("finding-a", "security", "fixed-pending-verification")]))

    assert module.validate_ledger(ledger) == []
    assert module.summarize_ledger(ledger) == {
        "status": "active",
        "reason": "baseline",
        "scores": [20],
        "rounds": [
            {
                "index": 1,
                "score": 20,
                "counts": {"security": 1, "critical": 0, "high": 0, "medium": 0, "low": 0, "nit": 0},
                "decision": "baseline",
            }
        ],
    }


def test_clean_requires_independent_review_of_current_snapshot() -> None:
    """Block a false clean result from an old reviewed revision."""
    module = _load_module()
    stale = _ledger(_round(1, [_finding("finding-a", disposition="verified-fixed")], _DIGEST_A), digest=_DIGEST_B)

    assert module.summarize_ledger(stale)["reason"] == "stale-review"

    current = _ledger(_round(1, [_finding("finding-a", disposition="verified-fixed")]))
    assert module.summarize_ledger(current)["reason"] == "clean"


def test_ratio_boundaries_and_round_cap_are_deterministic() -> None:
    """Continue at one-half, stop above it, and cap the third review round."""
    module = _load_module()
    half = _ledger(
        _round(1, [_finding("security-a", "security")]),
        _round(2, [_finding("security-a", "security", "verified-fixed"), _finding("critical-b", "critical")]),
    )
    plateau = _ledger(
        _round(1, [_finding("security-a", "security")]),
        _round(
            2,
            [
                _finding("security-a", "security", "verified-fixed"),
                _finding("high-b", "high"),
                _finding("medium-c", "medium"),
                _finding("low-d", "low"),
            ],
        ),
    )
    capped = _ledger(
        _round(1, [_finding("security-a", "security")]),
        _round(2, [_finding("security-a", "security", "verified-fixed"), _finding("critical-b", "critical")]),
        _round(
            3,
            [
                _finding("security-a", "security", "verified-fixed"),
                _finding("critical-b", "critical", "verified-fixed"),
                _finding("medium-c", "medium"),
            ],
        ),
    )

    assert module.summarize_ledger(half)["reason"] == "converging"
    assert module.summarize_ledger(plateau)["reason"] == "plateau"
    assert module.summarize_ledger(capped)["reason"] == "round-cap"


def test_structural_and_consecutive_open_findings_stop_immediately() -> None:
    """Prevent loop fixes for structural changes and unresolved repeated findings."""
    module = _load_module()
    structural = _ledger(_round(1, [_finding("contract-change", structural=True)]))
    pending = _ledger(
        _round(1, [_finding("finding-a", disposition="fixed-pending-verification")]),
        _round(2, [_finding("finding-a", disposition="fixed-pending-verification")]),
    )

    assert module.summarize_ledger(structural)["reason"] == "structural-finding"
    assert module.summarize_ledger(pending)["reason"] == "consecutive-open-finding"


@pytest.mark.parametrize(
    ("disposition", "decision", "score"),
    [
        pytest.param("rejected", "clean", 0, id="refuted-structural-finding"),
        pytest.param("verified-fixed", "clean", 0, id="verified-structural-carryover"),
        pytest.param("open", "structural-finding", 6, id="open-structural-finding"),
        pytest.param("fixed-pending-verification", "structural-finding", 6, id="unverified-structural-fix"),
    ],
)
def test_structural_stop_respects_verified_disposition(disposition: str, decision: str, score: int) -> None:
    """Retain closed structural evidence without blocking a separately approved correction's clean review."""
    module = _load_module()
    ledger = _ledger(_round(1, [_finding("contract-change", disposition=disposition, structural=True)]))

    summary = module.summarize_ledger(ledger)
    assert summary["reason"] == decision
    assert summary["scores"] == [score]


@pytest.mark.parametrize("later_disposition", ["open", "verified-fixed"])
def test_validator_rejects_a_round_after_a_terminal_decision(later_disposition: str) -> None:
    """Require a separately scoped run instead of extending a stopped ledger."""
    module = _load_module()
    continued = _ledger(
        _round(1, [_finding("contract-change", structural=True)]),
        _round(2, [_finding("contract-change", disposition=later_disposition, structural=True)]),
    )

    assert "round-after-stop" in module.validate_ledger(continued)


def test_history_is_stable_but_a_terminal_finding_can_reopen() -> None:
    """Require carried signatures while allowing later evidence of a real regression."""
    module = _load_module()
    reopened = _ledger(
        _round(1, [_finding("finding-a", disposition="verified-fixed"), _finding("finding-b", "medium")]),
        _round(2, [_finding("finding-a"), _finding("finding-b", "medium", "verified-fixed")]),
    )
    dropped = _ledger(
        _round(1, [_finding("finding-a")]),
        _round(2, []),
    )
    tier_changed = _ledger(
        _round(1, [_finding("finding-a", "high")]),
        _round(2, [_finding("finding-a", "low")]),
    )

    assert module.validate_ledger(reopened) == []
    assert "round-2-finding-dropped:finding-a" in module.validate_ledger(dropped)
    assert "round-2-finding-tier-changed:finding-a" in module.validate_ledger(tier_changed)


def test_missing_independent_coverage_is_a_valid_stopped_artifact() -> None:
    """Preserve an unavailable reviewer route as a reportable non-clean outcome."""
    module = _load_module()
    unavailable = _ledger(_round(1, [_finding("finding-a")], independent=False))

    assert module.validate_ledger(unavailable) == []
    assert module.summarize_ledger(unavailable)["reason"] == "independence-unavailable"
    assert module.summarize_ledger(_ledger())["reason"] == "independence-unavailable"


def test_unavailable_second_review_is_terminal_but_not_a_shape_error() -> None:
    """Retain the last independent score before an unavailable reviewer route stops work."""
    module = _load_module()
    stopped = _ledger(
        _round(1, [_finding("finding-a")]),
        _round(2, [_finding("finding-a")], independent=False),
    )
    continued = _ledger(
        _round(1, [_finding("finding-a")]),
        _round(2, [_finding("finding-a")], independent=False),
        _round(3, [_finding("finding-a")]),
    )

    assert module.validate_ledger(stopped) == []
    assert module.summarize_ledger(stopped)["reason"] == "independence-unavailable"
    assert "round-after-stop" in module.validate_ledger(continued)


@pytest.mark.parametrize(
    "mutate, expected_error",
    [
        pytest.param(
            lambda ledger: ledger.update({"schema_version": True}),
            "unsupported-schema-version",
            id="boolean-schema-version",
        ),
        pytest.param(
            lambda ledger: ledger["rounds"][0].update({"index": True}),
            "round-index-must-be-contiguous-integer",
            id="boolean-index",
        ),
        pytest.param(
            lambda ledger: ledger["rounds"][0]["reviewer"].update({"identity": "implementation-author"}),
            "round-1-self-review-forbidden",
            id="self-review",
        ),
        pytest.param(
            lambda ledger: ledger["current_snapshot"].update({"diff_digest": "ABC"}),
            "current-snapshot-diff-digest-invalid",
            id="invalid-digest",
        ),
        pytest.param(
            lambda ledger: ledger["rounds"][0]["findings"][0].update({"disposition": "invented"}),
            "round-1-finding-disposition-invalid",
            id="unexpected-disposition",
        ),
        pytest.param(
            lambda ledger: ledger["rounds"][0]["findings"][0].update({"disposition": "rejected"}),
            "round-1-rejected-finding-evidence-required",
            id="rejection-without-evidence",
        ),
        pytest.param(
            lambda ledger: ledger["rounds"][0]["findings"][0].update({"disposition": "verified-fixed"}),
            "round-1-verified-fixed-finding-evidence-required",
            id="verified-fix-without-evidence",
        ),
        pytest.param(
            lambda ledger: ledger["rounds"][0]["findings"][0].update({"tier": []}),
            "round-1-finding-tier-invalid",
            id="list-tier",
        ),
        pytest.param(
            lambda ledger: ledger["rounds"][0]["findings"][0].update({"disposition": {}}),
            "round-1-finding-disposition-invalid",
            id="object-disposition",
        ),
    ],
)
def test_validator_rejects_false_green_shape_and_value_claims(mutate: Any, expected_error: str) -> None:
    """Reject bools, self-review, malformed snapshots, and unsupported finding claims."""
    module = _load_module()
    ledger = _ledger(_round(1, [_finding("finding-a")]))
    mutate(ledger)

    assert expected_error in module.validate_ledger(ledger)
    with pytest.raises(ValueError, match=expected_error):
        module.summarize_ledger(ledger)


def test_real_cli_emits_valid_summary_and_keeps_invalid_output_off_stdout(tmp_path: Path) -> None:
    """Exercise the standalone CLI's JSON and exit-code contract."""
    valid_path = tmp_path / "valid.json"
    invalid_path = tmp_path / "invalid.json"
    valid_path.write_text(json.dumps(_ledger(_round(1, [_finding("finding-a")]))), encoding="utf-8", newline="\n")
    invalid_path.write_text(json.dumps({"schema_version": True}), encoding="utf-8", newline="\n")

    valid = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(valid_path)], capture_output=True, check=False, text=True
    )
    invalid = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(invalid_path)], capture_output=True, check=False, text=True
    )

    assert valid.returncode == 0
    assert json.loads(valid.stdout)["reason"] == "baseline"
    assert valid.stderr == ""
    assert invalid.returncode == 1
    assert invalid.stdout == ""
    assert json.loads(invalid.stderr)["reason"] == "invalid-ledger"


def test_cli_require_clean_fails_closed_but_preserves_valid_summary(tmp_path: Path) -> None:
    """Let a review gate reject valid-but-nonclean ledgers without hiding their result."""
    clean_path = tmp_path / "clean.json"
    blocked_path = tmp_path / "blocked.json"
    clean_ledger = _ledger(_round(1, [_finding("finding-a", disposition="verified-fixed")]))
    clean_path.write_text(json.dumps(clean_ledger), encoding="utf-8", newline="\n")
    blocked_path.write_text(json.dumps(_ledger()), encoding="utf-8", newline="\n")

    clean = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--require-clean", "--ledger", str(clean_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    normal_blocked = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(blocked_path)], capture_output=True, check=False, text=True
    )
    required_blocked = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--require-clean", "--ledger", str(blocked_path)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert clean.returncode == 0
    assert json.loads(clean.stdout)["reason"] == "clean"
    assert normal_blocked.returncode == 0
    assert json.loads(normal_blocked.stdout)["reason"] == "independence-unavailable"
    assert required_blocked.returncode == 1
    assert json.loads(required_blocked.stdout)["reason"] == "independence-unavailable"
    assert required_blocked.stderr == ""
