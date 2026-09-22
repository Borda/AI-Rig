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


def test_feasible_structural_and_repeated_findings_can_converge() -> None:
    """Keep authorized structural and repeated findings eligible for another challenge."""
    module = _load_module()
    structural = _ledger(_round(1, [_finding("contract-change", structural=True)]))
    repeated = _ledger(
        _round(1, [_finding("finding-a"), _finding("finding-b")]),
        _round(2, [_finding("finding-a"), _finding("finding-b", disposition="verified-fixed")]),
    )

    assert module.summarize_ledger(structural)["reason"] == "baseline"
    assert module.validate_ledger(repeated) == []
    assert module.summarize_ledger(repeated)["reason"] == "converging"


def test_actions_bind_every_open_finding_and_severe_escalation() -> None:
    """Reject silent omission or deferral of an unresolved high-severity finding."""
    module = _load_module()
    ledger = _ledger(_round(1, [_finding("boundary"), _finding("typo", "low")]))
    actions = {
        "schema_version": 1,
        "rounds": [
            {
                "index": 1,
                "actions": [
                    {
                        "signature": "boundary",
                        "decision": "escalate",
                        "evidence": ["Contract change exceeds current authority."],
                        "owner": "parent",
                        "next_action": "Request approval for the contract change.",
                        "root_cause": None,
                    },
                    {
                        "signature": "typo",
                        "decision": "defer",
                        "evidence": ["Copy change is outside the approved file set."],
                        "owner": "parent",
                        "next_action": "Request the copy file in scope.",
                        "root_cause": None,
                    },
                ],
            }
        ],
    }

    assert module.validate_actions(ledger, actions) == []
    actions["rounds"][0]["actions"].pop()
    assert "round-1-action-missing:typo" in module.validate_actions(ledger, actions)
    actions["rounds"][0]["actions"][0]["decision"] = "defer"
    assert "round-1-severe-finding-must-fix-or-escalate:boundary" in module.validate_actions(ledger, actions)


def test_repeated_open_finding_requires_root_cause_before_another_fix() -> None:
    """Tie a repeat-fix attempt to a falsifiable cause, not merely an improving score."""
    module = _load_module()
    ledger = _ledger(
        _round(1, [_finding("repeat")]),
        _round(2, [_finding("repeat"), _finding("new", "low")]),
    )
    action = {
        "signature": "repeat",
        "decision": "fix",
        "evidence": ["Changed parser boundary and added regression."],
        "owner": "parent",
        "next_action": "Request independent verification on the corrected snapshot.",
        "root_cause": None,
    }
    actions = {
        "schema_version": 1,
        "rounds": [
            {"index": 1, "actions": [action.copy()]},
            {
                "index": 2,
                "actions": [
                    action.copy(),
                    {
                        **action,
                        "signature": "new",
                        "decision": "defer",
                        "evidence": ["Low-severity copy change needs a separate scope."],
                    },
                ],
            },
        ],
    }

    assert "round-2-repeat-root-cause-required:repeat" in module.validate_actions(ledger, actions)
    actions["rounds"][1]["actions"][0]["root_cause"] = {
        "claim": "Parser fallback bypassed the boundary check.",
        "evidence": "The repeated report names the fallback path.",
        "falsification": "Run the fallback regression with the old branch.",
        "rejected_alternative": "Input corruption was excluded by the retained source snapshot.",
    }
    assert module.validate_actions(ledger, actions) == []


@pytest.mark.parametrize(
    ("disposition", "decision", "score"),
    [
        pytest.param("rejected", "clean", 0, id="refuted-structural-finding"),
        pytest.param("verified-fixed", "clean", 0, id="verified-structural-carryover"),
        pytest.param("open", "baseline", 6, id="open-structural-finding"),
        pytest.param("fixed-pending-verification", "baseline", 6, id="unverified-structural-fix"),
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
        _round(1, [_finding("contract-change", disposition="verified-fixed", structural=True)]),
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
        [sys.executable, str(LEDGER_PATH), "--ledger", str(invalid_path), "--progress"],
        capture_output=True,
        check=False,
        text=True,
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


def test_cli_progress_reprints_history_and_splits_old_new_weights(tmp_path: Path) -> None:
    """Append a reviewed row without changing earlier rows, JSON, or stop decisions."""
    ledger_path = tmp_path / "ledger.json"
    first = [_finding(tier, tier) for tier in ("security", "critical", "high", "medium", "low", "nit")]
    ledger = _ledger(_round(1, first))
    command = [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), "--progress", "--require-clean"]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    baseline = subprocess.run(command, capture_output=True, text=True, check=False)
    ledger["rounds"].append(
        _round(
            2,
            [
                _finding(
                    item["signature"],
                    item["tier"],
                    "fixed-pending-verification" if item["tier"] == "high" else "verified-fixed",
                )
                for item in first
            ]
            + [_finding(f"new-{index}") for index in range(3)],
        )
    )
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    updated = subprocess.run(command, capture_output=True, text=True, check=False)

    header = "| Iteration | Critical | High | Medium | Low | Nits | Weighted score |"
    first_row = "| 1 | 0 + 2 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 43 |"
    assert baseline.returncode == updated.returncode == 1
    assert header in baseline.stderr and header in updated.stderr
    assert first_row in baseline.stderr and first_row in updated.stderr
    assert "| 2 | 0 + 0 | 1 + 3 | 0 + 0 | 0 + 0 | 0 + 0 | 6 + 18 |" in updated.stderr
    assert json.loads(baseline.stdout) == _load_module().summarize_ledger(_ledger(_round(1, first)))
    assert json.loads(updated.stdout) == _load_module().summarize_ledger(ledger)
    assert json.loads(updated.stdout)["reason"] == "plateau"


def test_cli_progress_counts_reopened_signatures_as_old(tmp_path: Path) -> None:
    """Use signature history rather than just the previous round's open findings."""
    ledger = _ledger(
        _round(1, [_finding("prior", disposition="rejected"), _finding("initial", "security")]),
        _round(2, [_finding("prior"), _finding("initial", "security", "verified-fixed"), _finding("fresh", "nit")]),
    )
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), "--progress"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "| 1 | 0 + 1 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 20 |" in result.stderr
    assert "| 2 | 0 + 0 | 1 + 0 | 0 + 0 | 0 + 0 | 0 + 1 | 6 + 1 |" in result.stderr


@pytest.mark.parametrize("independent", [True, False])
def test_cli_progress_omits_table_before_any_completed_review(tmp_path: Path, independent: bool) -> None:
    """Keep unavailable review status factual without a placeholder table."""
    ledger = _ledger() if independent else _ledger(_round(1, [], independent=False))
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), "--progress"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout)["reason"] == "independence-unavailable"


@pytest.mark.parametrize("independent", [True, False])
def test_cli_progress_only_appends_completed_reviews(tmp_path: Path, independent: bool) -> None:
    """Show a clean zero row only for independent coverage, retaining prior history either way."""
    ledger = _ledger(
        _round(1, [_finding("prior")]),
        _round(2, [_finding("prior", disposition="verified-fixed")], independent=independent),
    )
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), "--progress", "--require-clean"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if independent else 1)
    assert "| 1 | 0 + 0 | 0 + 1 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 6 |" in result.stderr
    assert ("| 2 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 |" in result.stderr) is independent
    assert json.loads(result.stdout)["reason"] == ("clean" if independent else "independence-unavailable")


@pytest.mark.parametrize("completed_reviews", [0, 1, 2])
def test_cli_progress_separates_legend_from_markdown_table(tmp_path: Path, completed_reviews: int) -> None:
    """Keep the legend out of the rendered table for empty and cumulative histories."""
    rounds = [
        _round(1, [_finding("prior")]),
        _round(2, [_finding("prior", disposition="verified-fixed")]),
    ]
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(_ledger(*rounds[:completed_reviews])), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), "--progress"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    table, separator, legend = result.stderr.partition("\n\n")
    if completed_reviews == 0:
        assert result.stderr == ""
    else:
        assert separator == "\n\n"
        assert len(table.splitlines()) == 2 + completed_reviews
        assert all(line.startswith("| ") and line.endswith(" |") for line in table.splitlines())
        assert legend.startswith("Cells: old + new open findings")
        assert "Cells:" not in table
