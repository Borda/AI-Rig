"""Regression checks for the deterministic adversarial-review convergence ledger."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = PLUGIN_ROOT / "shared" / "adversarial_loop.py"


def _load_module() -> ModuleType:
    """Load the standalone convergence-ledger helper without installation."""
    specification = importlib.util.spec_from_file_location("codex_rig_adversarial_loop", LEDGER_PATH)
    assert specification is not None
    assert specification.loader is not None
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


def test_score_decreases_remain_active_after_third_round() -> None:
    """Continue after any decrease without a fixed review-round cap."""
    module = _load_module()
    half = _ledger(
        _round(1, [_finding("security-a", "security")]),
        _round(2, [_finding("security-a", "security", "verified-fixed"), _finding("critical-b", "critical")]),
    )
    smaller_decrease = _ledger(
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
    improving = _ledger(
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
    assert module.summarize_ledger(smaller_decrease)["reason"] == "converging"
    assert module.summarize_ledger(improving)["reason"] == "converging"


@pytest.mark.parametrize(
    ("tier", "reason"),
    [
        pytest.param("high", "plateau", id="unchanged-score"),
        pytest.param("critical", "nonconverging", id="increased-score"),
    ],
)
def test_unchanged_and_increased_scores_stop(tier: str, reason: str) -> None:
    """Stop true score plateaus and regressions without confusing a smaller decrease."""
    ledger = _ledger(
        _round(1, [_finding("initial", "high")]),
        _round(2, [_finding("initial", "high", "verified-fixed"), _finding("remaining", tier)]),
    )
    assert _load_module().summarize_ledger(ledger)["reason"] == reason


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


def test_current_fix_action_requires_invariant_and_sibling_evidence() -> None:
    """Block a fix record that proves only the original example."""
    module = _load_module()
    ledger = _ledger(_round(1, [_finding("external-write")]))
    action = {
        "signature": "external-write",
        "decision": "fix",
        "evidence": [
            "invariant: release preparation cannot write outside its directory",
            "original: release-directory symlink regression failed before and passed after",
            "consumer: setup release command returned an error before any external write",
            "source-paths: setup_release_dir.py and its regression test",
        ],
        "owner": "parent",
        "next_action": "Seek a fresh independent challenge.",
        "root_cause": None,
    }
    actions = {"schema_version": 2, "rounds": [{"index": 1, "actions": [action]}]}

    assert "round-1-fix-evidence-missing:sibling:external-write" in module.validate_actions(ledger, actions)
    action["evidence"].append("sibling: existing backup symlink failed before and passed after")
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
    ("mutate", "expected_error"),
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

    header = "| Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score |"
    first_row = "| 1 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 43 |"
    assert baseline.returncode == updated.returncode == 1
    assert header in baseline.stderr
    assert header in updated.stderr
    assert first_row in baseline.stderr
    assert first_row in updated.stderr
    assert "| 2 | 0 + 0 | 0 + 0 | 1 + 3 | 0 + 0 | 0 + 0 | 0 + 0 | 6 + 18 |" in updated.stderr
    assert json.loads(baseline.stdout) == _load_module().summarize_ledger(_ledger(_round(1, first)))
    assert json.loads(updated.stdout) == _load_module().summarize_ledger(ledger)
    assert json.loads(updated.stdout)["reason"] == "converging"


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
    assert "| 1 | 0 + 1 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 20 |" in result.stderr
    assert "| 2 | 0 + 0 | 0 + 0 | 1 + 0 | 0 + 0 | 0 + 0 | 0 + 1 | 6 + 1 |" in result.stderr


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
    assert "| 1 | 0 + 0 | 0 + 0 | 0 + 1 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 6 |" in result.stderr
    assert ("| 2 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 |" in result.stderr) is independent
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


def _write_header(path: Path, digest: str = _DIGEST_A) -> None:
    """Write a current schema-2 ledger header with no completed round."""
    header = {key: value for key, value in _ledger(digest=digest).items() if key != "rounds"} | {"schema_version": 2}
    path.write_text(json.dumps(header), encoding="utf-8", newline="\n")


def _append(ledger_path: Path, round_record: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """Stage one round beside the header and append it through the CLI, as the lifecycle owner does."""
    (ledger_path.parent / "loop-rounds.jsonl.rec").write_text(json.dumps(round_record, indent=2), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), "--append", "--progress"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_append_round_trips_rounds_without_rewriting_earlier_lines(tmp_path: Path) -> None:
    """Grow the round log one staged round at a time and read it back as the assembled ledger.

    The second append must leave the first round's line byte-identical: the log is only appended, so a dropped or
    reworded earlier round can never be introduced by recording a later one.
    """
    module = _load_module()
    ledger_path = tmp_path / "loop-ledger.json"
    _write_header(ledger_path)
    first = _round(1, [_finding("finding-a")])
    second = _round(2, [_finding("finding-a", disposition="verified-fixed")])

    baseline = _append(ledger_path, first)
    first_line = (tmp_path / "loop-rounds.jsonl").read_bytes()
    final = _append(ledger_path, second)

    log = (tmp_path / "loop-rounds.jsonl").read_bytes()
    assert baseline.returncode == 0, baseline.stderr
    assert final.returncode == 0, final.stderr
    assert log.startswith(first_line)
    assert len(log.splitlines()) == 2
    assert not (tmp_path / "loop-rounds.jsonl.rec").exists()
    assert module.load_ledger(ledger_path) == _ledger(first, second) | {"schema_version": 2}
    assert json.loads(final.stdout) == module.summarize_ledger(_ledger(first, second))
    assert "| 2 | 0 + 0 |" in final.stderr


@pytest.mark.parametrize(
    ("staged", "expected_error"),
    [
        pytest.param(_round(3, [_finding("finding-a")]), "round-index-must-be-contiguous-integer", id="skipped-index"),
        pytest.param(_round(1, []), "round-index-must-be-contiguous-integer", id="repeated-index"),
        pytest.param(_round(2, []), "round-2-finding-dropped:finding-a", id="dropped-signature"),
    ],
)
def test_append_refuses_invalid_round_and_keeps_staged_record(
    tmp_path: Path, staged: dict[str, Any], expected_error: str
) -> None:
    """Reject a staged round that would break history, appending nothing and keeping the record for repair."""
    ledger_path = tmp_path / "loop-ledger.json"
    _write_header(ledger_path)
    assert _append(ledger_path, _round(1, [_finding("finding-a")])).returncode == 0
    before = (tmp_path / "loop-rounds.jsonl").read_bytes()
    rejected = _append(ledger_path, staged)

    assert rejected.returncode == 1
    assert rejected.stdout == ""
    assert json.loads(rejected.stderr)["reason"] == "append-rejected"
    assert expected_error in json.loads(rejected.stderr)["errors"]
    assert (tmp_path / "loop-rounds.jsonl").read_bytes() == before
    assert (tmp_path / "loop-rounds.jsonl.rec").is_file()


def test_historical_inline_ledger_stays_readable_but_cannot_grow(tmp_path: Path) -> None:
    """Read a schema-1 archive unchanged, while refusing to append rounds to the rewritten single-file shape."""
    module = _load_module()
    ledger_path = tmp_path / "loop-ledger.json"
    historical = _ledger(_round(1, [_finding("finding-a")]))
    ledger_path.write_text(json.dumps(historical), encoding="utf-8", newline="\n")

    appended = _append(ledger_path, _round(2, [_finding("finding-a")]))

    assert module.load_ledger(ledger_path) == historical
    assert module.validate_ledger(module.load_ledger(ledger_path)) == []
    assert appended.returncode == 1
    assert "append-requires-current-schema" in json.loads(appended.stderr)["errors"]
    assert json.loads(ledger_path.read_text(encoding="utf-8")) == historical


def test_current_header_with_inline_rounds_is_unreadable(tmp_path: Path) -> None:
    """Reject a schema-2 header that also carries rounds, because two copies of history could disagree."""
    ledger_path = tmp_path / "loop-ledger.json"
    ledger_path.write_text(json.dumps(_ledger() | {"schema_version": 2}), encoding="utf-8", newline="\n")

    result = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert json.loads(result.stderr)["reason"] == "ledger-read-failed"
    assert "ledger-header-inline-rounds-forbidden" in result.stderr


def test_ledger_digest_binds_historical_bytes_and_current_round_log(tmp_path: Path) -> None:
    """Keep archived continuation digests valid and make any round-log change visible in the current digest."""
    module = _load_module()
    historical = tmp_path / "historical" / "loop-ledger.json"
    historical.parent.mkdir()
    historical.write_bytes(json.dumps(_ledger(_round(1, [_finding("finding-a")]))).encode())
    current = tmp_path / "current" / "loop-ledger.json"
    current.parent.mkdir()
    _write_header(current)
    empty_digest = module.ledger_digest(current)
    assert _append(current, _round(1, [_finding("finding-a")])).returncode == 0
    one_round_digest = module.ledger_digest(current)
    rounds_log = current.with_name("loop-rounds.jsonl")
    rounds_log.write_bytes(rounds_log.read_bytes() + b" ")

    assert module.ledger_digest(historical) == hashlib.sha256(historical.read_bytes()).hexdigest()
    assert len({empty_digest, one_round_digest, module.ledger_digest(current)}) == 3


@pytest.mark.integration
def test_five_improving_rounds_append_to_clean_without_rewriting_history(tmp_path: Path) -> None:
    """Permit a monotone five-round correction while preserving append-only identity and clean gating."""
    module = _load_module()
    ledger_path = tmp_path / "loop-ledger.json"
    _write_header(ledger_path)
    tiers = ["security", "critical", "high", "medium"]
    expected_scores = [40, 20, 10, 4, 0]
    prior_bytes = b""
    for index in range(1, 6):
        findings = [
            _finding(f"finding-{number}", tier, "verified-fixed" if number < index else "open")
            for number, tier in enumerate(tiers, start=1)
        ]
        result = _append(ledger_path, _round(index, findings))
        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout)
        assert summary["scores"] == expected_scores[:index]
        assert summary["reason"] == ("baseline" if index == 1 else "clean" if index == 5 else "converging")
        current_bytes = module.rounds_path(ledger_path).read_bytes()
        assert current_bytes.startswith(prior_bytes)
        assert len(current_bytes.splitlines()) == index
        prior_bytes = current_bytes
    clean = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), "--require-clean"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert clean.returncode == 0, clean.stderr
    assert json.loads(clean.stdout)["reason"] == "clean"
    assert module.load_ledger(ledger_path)["schema_version"] == module.SCHEMA_VERSION == 2
    after_clean = _append(ledger_path, _round(6, findings))
    assert after_clean.returncode == 1
    assert "round-after-stop" in json.loads(after_clean.stderr)["errors"]
    assert module.rounds_path(ledger_path).read_bytes() == prior_bytes


@pytest.mark.parametrize(
    ("tier", "reason"),
    [
        pytest.param("medium", "plateau", id="late-plateau"),
        pytest.param("high", "nonconverging", id="late-increase"),
    ],
)
def test_late_nonimprovement_stops_and_refuses_a_further_round(tier: str, reason: str) -> None:
    """Removing the cardinality limit never permits extension after a late score stop."""
    module = _load_module()
    history = [
        _round(1, [_finding("a", "security")]),
        _round(2, [_finding("a", "security", "verified-fixed"), _finding("b", "critical")]),
        _round(
            3,
            [
                _finding("a", "security", "verified-fixed"),
                _finding("b", "critical", "verified-fixed"),
                _finding("c", "medium"),
            ],
        ),
    ]
    latest = [
        _finding("a", "security", "verified-fixed"),
        _finding("b", "critical", "verified-fixed"),
        _finding("c", "medium", "verified-fixed"),
        _finding("d", tier),
    ]
    stopped = _ledger(*history, _round(4, latest)) | {"schema_version": 2}
    assert module.validate_ledger(stopped) == []
    assert module.summarize_ledger(stopped)["reason"] == reason
    continued = {
        **stopped,
        "rounds": [
            *stopped["rounds"],
            _round(5, [dict(item, disposition="verified-fixed", evidence=["verified"]) for item in latest]),
        ],
    }
    assert "round-after-stop" in module.validate_ledger(continued)


def test_reopened_third_occurrence_cannot_be_fixed_despite_improving_total() -> None:
    """Count reopened signatures cumulatively so closing other findings cannot hide a third recurrence."""
    module = _load_module()
    tiers = {"repeat": "low", "a": "security", "b": "critical", "c": "high", "d": "medium", "e": "nit"}
    closed_by_round = [set(), {"repeat", "a"}, {"a", "b"}, {"repeat", "a", "b", "c"}, {"a", "b", "c", "d"}]
    rounds = [
        _round(
            index,
            [
                _finding(signature, tier, "verified-fixed" if signature in closed else "open")
                for signature, tier in tiers.items()
            ],
        )
        for index, closed in enumerate(closed_by_round, start=1)
    ]
    ledger = _ledger(*rounds) | {"schema_version": 2}
    actions = {
        "schema_version": 1,
        "rounds": [
            {
                "index": index,
                "actions": [
                    {
                        "signature": signature,
                        "decision": "fix" if signature == "repeat" else "escalate",
                        "evidence": ["Retained independent recurrence evidence"],
                        "owner": "parent",
                        "next_action": "Seek independent verification",
                        # a reopened invariant needs a cause before each later fix; this test isolates the third stop
                        "root_cause": _ROOT_CAUSE if signature == "repeat" else None,
                    }
                    for signature in tiers
                    if signature not in closed
                ],
            }
            for index, closed in enumerate(closed_by_round, start=1)
        ],
    }
    assert module.validate_ledger(ledger) == []
    assert module.summarize_ledger(ledger)["scores"] == [43, 21, 13, 5, 3]
    assert module.validate_actions(ledger, actions) == ["round-5-third-occurrence-must-stop:repeat"]


_ROOT_CAUSE = {
    "claim": "Reader default admits what it does not model.",
    "evidence": "Retained source read names the default branch.",
    "falsification": "A refusing default rejects the original case.",
    "rejected_alternative": "Listing the reported route closes only that route.",
}


def _invariant_action(signature: str, root_cause: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a schema-2 fix action whose evidence names one shared invariant."""
    return {
        "signature": signature,
        "decision": "fix",
        "evidence": [
            "invariant: Every case arm is judged as a command",
            "original: retained case file c1",
            "consumer: hook verdict",
            "sibling: retained case file c2",
            "source-paths: reader.py",
        ],
        "owner": "parent",
        "next_action": "Seek independent verification",
        "root_cause": root_cause,
    }


def _route_ledger(*signatures_by_round: list[str], invariants: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a schema-2 ledger where each round reports the given open security signatures.

    Every signature from an earlier round that is not open again is carried as verified-fixed, as the ledger requires. A
    signature named in ``invariants`` carries that text as its finding's ``invariant:`` evidence line.
    """
    invariants = invariants or {}
    seen: list[str] = []
    rounds = []
    for index, signatures in enumerate(signatures_by_round, start=1):
        seen += [signature for signature in signatures if signature not in seen]
        rounds.append(
            _round(
                index,
                [
                    _finding(
                        signature,
                        "security",
                        "open" if signature in signatures else "verified-fixed",
                        evidence=[f"invariant: {invariants[signature]}"] if signature in invariants else None,
                    )
                    for signature in seen
                ],
            )
        )
    return _ledger(*rounds) | {"schema_version": 2}


def _actions(*rounds: list[dict[str, Any]], version: int = 2) -> dict[str, Any]:
    """Wrap per-round action lists in an actions document, schema 2 unless a version is given."""
    return {"schema_version": version, "rounds": [{"index": i, "actions": a} for i, a in enumerate(rounds, start=1)]}


@pytest.mark.parametrize("version", [1, 2, 3])
def test_action_only_invariant_cannot_connect_distinct_ledger_findings(version: int) -> None:
    """Only schema 3 rejects an action-only connection that progress cannot see in ledger evidence."""
    module = _load_module()
    ledger = _route_ledger(["a"], ["b"], invariants={"a": "first boundary", "b": "second boundary"})
    actions = _actions(
        [_reworded_action("a", "shared boundary")],
        [_reworded_action("b", "shared boundary", _ROOT_CAUSE)],
        version=version,
    )

    assert module.validate_ledger(ledger) == []
    assert "| 2 | 0 + 1 |" in module._render_progress(ledger, 2)
    expected = (
        ["round-1-action-invariant-not-in-finding:a", "round-2-action-invariant-not-in-finding:b"]
        if version == 3
        else []
    )
    assert module.validate_actions(ledger, actions) == expected


@pytest.mark.parametrize("decision", ["fix", "escalate", "defer"])
@pytest.mark.parametrize(
    ("finding_evidence", "expected"),
    [
        pytest.param(
            ["invariant: Straße  boundary", "invariant: second boundary", "invariant: ledger-only boundary"],
            [],
            id="normalized-subset",
        ),
        pytest.param(
            ["invariant: Straße boundary"],
            ["round-1-action-invariant-not-in-finding:boundary"],
            id="second-invariant-missing",
        ),
        pytest.param([], ["round-1-action-invariant-not-in-finding:boundary"], id="no-ledger-invariant"),
    ],
)
def test_current_actions_require_every_invariant_in_finding(
    decision: str, finding_evidence: list[str], expected: list[str]
) -> None:
    """Every action invariant must match normalized finding evidence, for all schema-3 decisions."""
    module = _load_module()
    ledger = _ledger(_round(1, [_finding("boundary", "low", evidence=finding_evidence)]))
    action = _invariant_action("boundary")
    action["decision"] = decision
    action["evidence"][:1] = ["invariant:  STRASSE boundary ", "invariant: second  BOUNDARY"]

    assert module.validate_actions(ledger, _actions([action], version=3)) == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        pytest.param(1, [], id="schema-one-signature-only"),
        pytest.param(2, [], id="schema-two-signature-only"),
        pytest.param(3, ["round-2-repeat-root-cause-required:route-two"], id="schema-three-invariant-repeat"),
    ],
)
def test_new_route_to_known_invariant_requires_root_cause(version: int, expected: list[str]) -> None:
    """Only schema 3 treats a sibling signature with the same invariant as a repeat.

    A filter loop named each round's bypass after its route, so the signature never repeated and the repeat root-cause
    rule never fired; schema 3 uses the shared ``invariant:`` evidence to identify that recurrence.
    """
    module = _load_module()
    ledger = _route_ledger(
        ["route-one"],
        ["route-two"],
        invariants=dict.fromkeys(("route-one", "route-two"), "Every case arm is judged as a command"),
    )
    actions = _actions([_invariant_action("route-one")], [_invariant_action("route-two")], version=version)

    assert module.validate_actions(ledger, actions) == expected


@pytest.mark.parametrize("version", [1, 2])
def test_historical_signature_repeat_requires_root_cause(version: int) -> None:
    """A fix of a signature open in the previous round needs root-cause evidence in historical schemas."""
    module = _load_module()
    ledger = _route_ledger(["repeat"], ["repeat"])
    actions = _actions([_invariant_action("repeat")], [_invariant_action("repeat")], version=version)

    assert module.validate_actions(ledger, actions) == ["round-2-repeat-root-cause-required:repeat"]


@pytest.mark.parametrize("version", [1, 2])
def test_historical_signature_reopened_after_a_gap_is_not_a_repeat(version: int) -> None:
    """One earlier occurrence before an intervening closed round does not require root-cause evidence."""
    module = _load_module()
    ledger = _route_ledger(["repeat", "padding"], ["padding"], ["repeat"])
    actions = _actions(
        [_invariant_action("repeat"), _held_action("padding", "unrelated")],
        [_held_action("padding", "unrelated")],
        [_invariant_action("repeat")],
        version=version,
    )

    assert module.validate_actions(ledger, actions) == []


@pytest.mark.parametrize("version", [1, 2])
def test_historical_occurrences_include_findings_without_actions(version: int) -> None:
    """Missing actions do not erase open signatures from the repeat or third-occurrence count."""
    module = _load_module()
    ledger = _route_ledger(["repeat", "pad-1", "pad-2"], ["repeat", "pad-1"], ["repeat"])
    actions = _actions(
        [_held_action("pad-1", "padding one"), _held_action("pad-2", "padding two")],
        [_held_action("pad-1", "padding one")],
        [_invariant_action("repeat")],
        version=version,
    )

    assert module.validate_actions(ledger, actions) == [
        "round-1-action-missing:repeat",
        "round-2-action-missing:repeat",
        "round-3-repeat-root-cause-required:repeat",
        "round-3-third-occurrence-must-stop:repeat",
    ]


def test_third_route_to_known_invariant_must_stop() -> None:
    """Three route-named signatures of one invariant count as its third occurrence."""
    module = _load_module()
    # escalated padding keeps the score strictly decreasing, so round 3 is not refused as a round after a plateau stop
    ledger = _route_ledger(
        ["route-one", "pad-1", "pad-2"],
        ["route-two", "pad-1"],
        ["route-three"],
        invariants={
            **dict.fromkeys(("route-one", "route-two", "route-three"), "Every case arm is judged as a command"),
            "pad-1": "padding one",
            "pad-2": "padding two",
        },
    )
    actions = _actions(
        [_invariant_action("route-one"), _held_action("pad-1", "padding one"), _held_action("pad-2", "padding two")],
        [_invariant_action("route-two", _ROOT_CAUSE), _held_action("pad-1", "padding one")],
        [_invariant_action("route-three", _ROOT_CAUSE)],
        version=3,
    )

    assert module.validate_actions(ledger, actions) == ["round-3-third-occurrence-must-stop:route-three"]


def test_invariant_reopened_after_closed_round_is_third_occurrence() -> None:
    """A verified-fixed carry still enters history, so reopening after two recorded rounds must stop."""
    module = _load_module()
    ledger = _route_ledger(
        ["route-one", "other"],
        ["other"],
        ["route-two"],
        invariants={
            **dict.fromkeys(("route-one", "route-two"), "Every case arm is judged as a command"),
            "other": "unrelated",
        },
    )
    other = {
        **_invariant_action("other", _ROOT_CAUSE),
        "evidence": ["invariant: unrelated", *_invariant_action("x")["evidence"][1:]],
    }
    actions = _actions(
        [_invariant_action("route-one"), {**other, "root_cause": None}],
        [other],
        [_invariant_action("route-two")],
        version=3,
    )

    assert module.validate_actions(ledger, actions) == [
        "round-3-repeat-root-cause-required:route-two",
        "round-3-third-occurrence-must-stop:route-two",
    ]
    actions["rounds"][2]["actions"][0]["root_cause"] = _ROOT_CAUSE
    assert module.validate_actions(ledger, actions) == ["round-3-third-occurrence-must-stop:route-two"]


def _held_action(signature: str, invariant: str, decision: str = "escalate") -> dict[str, Any]:
    """Build a schema-3 action that does not fix its finding but still names the invariant it violates."""
    return {
        "signature": signature,
        "decision": decision,
        "evidence": [f"invariant: {invariant}", "Contract change needs an owner decision."],
        "owner": "parent",
        "next_action": "Request the owner decision.",
        "root_cause": None,
    }


def _reworded_action(signature: str, invariant: str, root_cause: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a schema-3 fix action whose ``invariant:`` evidence carries the given wording."""
    action = _invariant_action(signature, root_cause)
    return {**action, "evidence": [f"invariant: {invariant}", *action["evidence"][1:]]}


def test_reworded_invariant_under_known_signature_is_repeat() -> None:
    """A known signature stays a repeat when the parent rewords its invariant in a later round.

    Keying recurrence on the invariant text alone let one rewording hide the second fix of the same finding, a
    regression from signature-keyed counting; the signature is still part of the identity.
    """
    module = _load_module()
    ledger = _route_ledger(
        ["case-arm-body-judged"],
        ["case-arm-body-judged"],
        invariants={"case-arm-body-judged": "every arm judged"},
    )
    ledger["rounds"][1]["findings"][0]["evidence"] = ["invariant: arms are judged"]
    actions = _actions(
        [_reworded_action("case-arm-body-judged", "every arm judged")],
        [_reworded_action("case-arm-body-judged", "arms are judged")],
        version=3,
    )

    assert module.validate_actions(ledger, actions) == ["round-2-repeat-root-cause-required:case-arm-body-judged"]


@pytest.mark.parametrize("decision", ["escalate", "defer"])
def test_new_route_after_unfixed_invariant_is_repeat(decision: str) -> None:
    """An invariant first escalated or deferred still counts as an earlier occurrence when a new route is fixed.

    Only fixes had to name their invariant, so a round that escalated the finding left no key and the next route read as
    a first occurrence. Schema 3 makes every decision carry the ``invariant:`` entry the recurrence count needs.
    """
    module = _load_module()
    ledger = _route_ledger(
        ["route-one"],
        ["route-two"],
        invariants=dict.fromkeys(("route-one", "route-two"), "Every case arm is judged as a command"),
    )
    # route-one gets a deferrable tier, carried unchanged into round 2 as the ledger requires
    ledger["rounds"][0]["findings"][0]["tier"] = ledger["rounds"][1]["findings"][0]["tier"] = "medium"
    actions = _actions(
        [_held_action("route-one", "Every case arm is judged as a command", decision)],
        [_invariant_action("route-two")],
        version=3,
    )

    assert module.validate_actions(ledger, actions) == ["round-2-repeat-root-cause-required:route-two"]


def test_shared_invariant_within_one_round_is_one_occurrence() -> None:
    """Two signatures of one invariant in the same round are accepted, neither a repeat nor an error.

    A reviewer can report two routes to one invariant at once; the parent fixes both under one closure plan, and the
    round is that invariant's first occurrence.
    """
    module = _load_module()
    ledger = _route_ledger(
        ["route-one", "route-two"],
        invariants=dict.fromkeys(("route-one", "route-two"), "Every case arm is judged as a command"),
    )
    actions = _actions([_invariant_action("route-one"), _invariant_action("route-two")], version=3)

    assert module.validate_actions(ledger, actions) == []


def test_shared_invariant_round_counts_once_toward_third_occurrence() -> None:
    """A round where two routes share an invariant is one occurrence, so the third fix is two rounds later, not one.

    Round 1 records the invariant twice, round 2 fixes a new route with root cause as its second occurrence, and round 3
    is the first round with two earlier rounds behind it.
    """
    module = _load_module()
    # escalated padding keeps the score strictly decreasing, so round 3 is not refused as a round after a plateau stop
    ledger = _route_ledger(
        ["route-one", "route-two", "pad-1", "pad-2"],
        ["route-three", "pad-1"],
        ["route-four"],
        invariants={
            **dict.fromkeys(
                ("route-one", "route-two", "route-three", "route-four"), "Every case arm is judged as a command"
            ),
            "pad-1": "pad one holds",
            "pad-2": "pad two holds",
        },
    )
    actions = _actions(
        [
            _invariant_action("route-one"),
            _invariant_action("route-two"),
            _held_action("pad-1", "pad one holds"),
            _held_action("pad-2", "pad two holds"),
        ],
        [_invariant_action("route-three", _ROOT_CAUSE), _held_action("pad-1", "pad one holds")],
        [_invariant_action("route-four", _ROOT_CAUSE)],
        version=3,
    )

    assert module.validate_actions(ledger, actions) == ["round-3-third-occurrence-must-stop:route-four"]


def test_keys_from_two_earlier_rounds_make_a_third_occurrence() -> None:
    """A fix whose signature matches one earlier round and whose invariant matches another is a third occurrence.

    Counting each key on its own would see one earlier round per key; the earlier occurrences are the union of the
    rounds any identity key appeared in.
    """
    module = _load_module()
    ledger = _route_ledger(
        ["sig-a", "pad-1", "pad-2"],
        ["sig-b", "pad-1"],
        ["sig-a"],
        invariants={
            "sig-a": "arms are judged",
            "sig-b": "options are judged",
            "pad-1": "pad one holds",
            "pad-2": "pad two holds",
        },
    )
    ledger["rounds"][2]["findings"][0]["evidence"] = ["invariant: options are judged"]
    actions = _actions(
        [
            _reworded_action("sig-a", "arms are judged"),
            _held_action("pad-1", "pad one holds"),
            _held_action("pad-2", "pad two holds"),
        ],
        [_reworded_action("sig-b", "options are judged"), _held_action("pad-1", "pad one holds")],
        [_reworded_action("sig-a", "options are judged", _ROOT_CAUSE)],
        version=3,
    )

    assert module.validate_actions(ledger, actions) == ["round-3-third-occurrence-must-stop:sig-a"]


@pytest.mark.parametrize(
    ("signature", "invariant", "root_cause", "expected"),
    [
        pytest.param(
            "route-b",
            "second wording",
            _ROOT_CAUSE,
            ["round-3-third-occurrence-must-stop:route-b"],
            id="chain-third-occurrence",
        ),
        pytest.param(
            "route-b",
            "first wording",
            _ROOT_CAUSE,
            ["round-3-third-occurrence-must-stop:route-b"],
            id="chain-back-to-first-key",
        ),
        pytest.param("route-c", "unrelated invariant", None, [], id="unrelated-key-is-new"),
    ],
)
def test_recurrence_follows_keys_connected_by_earlier_actions(
    signature: str, invariant: str, root_cause: dict[str, str] | None, expected: list[str]
) -> None:
    """Rewording a known signature joins both invariant keys before the next route is counted."""
    module = _load_module()
    ledger = _route_ledger(
        ["route-a", "pad-1", "pad-2"],
        ["route-a", "pad-1"],
        [signature],
        invariants={
            "route-a": "first wording",
            "pad-1": "pad one holds",
            "pad-2": "pad two holds",
            signature: invariant,
        },
    )
    ledger["rounds"][1]["findings"][0]["evidence"] = ["invariant: second wording"]
    actions = _actions(
        [
            _reworded_action("route-a", "first wording"),
            _held_action("pad-1", "pad one holds"),
            _held_action("pad-2", "pad two holds"),
        ],
        [_reworded_action("route-a", "second wording", _ROOT_CAUSE), _held_action("pad-1", "pad one holds")],
        [_reworded_action(signature, invariant, root_cause)],
        version=3,
    )

    assert module.validate_actions(ledger, actions) == expected


def test_two_round_key_chain_is_only_a_repeat() -> None:
    """A chain formed by two same-round actions has only one earlier round behind the next fix."""
    module = _load_module()
    ledger = _route_ledger(
        ["route-a", "route-b"],
        ["route-c"],
        invariants={"route-a": "first wording", "route-b": "second wording", "route-c": "first wording"},
    )
    ledger["rounds"][0]["findings"][0]["evidence"].append("invariant: second wording")
    first = _reworded_action("route-a", "first wording")
    first["evidence"].append("invariant: second wording")
    actions = _actions(
        [first, _reworded_action("route-b", "second wording")],
        [_reworded_action("route-c", "first wording")],
        version=3,
    )

    assert module.validate_actions(ledger, actions) == ["round-2-repeat-root-cause-required:route-c"]
    actions["rounds"][1]["actions"][0]["root_cause"] = _ROOT_CAUSE
    assert module.validate_actions(ledger, actions) == []


def test_current_round_connections_do_not_change_sibling_recurrence() -> None:
    """A connection created by one action is published only after its siblings have been validated."""
    module = _load_module()
    ledger = _route_ledger(
        ["route-a", "pad-1", "pad-2"],
        ["route-a", "route-b"],
        invariants={
            "route-a": "first wording",
            "route-b": "second wording",
            "pad-1": "pad one holds",
            "pad-2": "pad two holds",
        },
    )
    ledger["rounds"][1]["findings"][0]["evidence"] = ["invariant: second wording"]
    actions = _actions(
        [
            _reworded_action("route-a", "first wording"),
            _held_action("pad-1", "pad one holds"),
            _held_action("pad-2", "pad two holds"),
        ],
        [_reworded_action("route-a", "second wording", _ROOT_CAUSE), _reworded_action("route-b", "second wording")],
        version=3,
    )

    assert module.validate_actions(ledger, actions) == []


@pytest.mark.parametrize("version", [1, 2])
def test_historical_actions_keep_signature_only_recurrence(version: int) -> None:
    """Archived action schemas do not connect signatures through shared invariant evidence."""
    module = _load_module()
    ledger = _route_ledger(["route-a", "pad-1", "pad-2"], ["route-a", "pad-1"], ["route-b"])
    actions = _actions(
        [
            _reworded_action("route-a", "first wording"),
            _held_action("pad-1", "pad one holds"),
            _held_action("pad-2", "pad two holds"),
        ],
        [_reworded_action("route-a", "second wording", _ROOT_CAUSE), _held_action("pad-1", "pad one holds")],
        [_reworded_action("route-b", "second wording", _ROOT_CAUSE)],
        version=version,
    )

    assert module.validate_actions(ledger, actions) == []


@pytest.mark.parametrize("version", [1, 2, 3])
def test_ledger_invariants_count_when_actions_name_unrelated_invariants(version: int) -> None:
    """Schema 3 rejects unrelated action wording and still counts the ledger's invariant history."""
    module = _load_module()
    ledger = _route_ledger(
        ["a", "pad-1", "pad-2"],
        ["b", "pad-1"],
        ["c"],
        invariants={
            "a": "Shared invariant",
            "b": " shared  INVARIANT ",
            "c": "shared invariant",
            "pad-1": "padding one",
            "pad-2": "padding two",
        },
    )
    actions = _actions(
        [
            _reworded_action("a", "first unrelated invariant"),
            _held_action("pad-1", "padding one"),
            _held_action("pad-2", "padding two"),
        ],
        [_reworded_action("b", "second unrelated invariant"), _held_action("pad-1", "padding one")],
        [_reworded_action("c", "third unrelated invariant")],
        version=version,
    )

    assert module.validate_ledger(ledger) == []
    progress = module._render_progress(ledger, 3)
    assert "| 2 | 2 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 40 + 0 |" in progress
    assert "| 3 | 1 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 20 + 0 |" in progress
    expected = (
        [
            "round-1-action-invariant-not-in-finding:a",
            "round-2-action-invariant-not-in-finding:b",
            "round-2-repeat-root-cause-required:b",
            "round-3-action-invariant-not-in-finding:c",
            "round-3-repeat-root-cause-required:c",
            "round-3-third-occurrence-must-stop:c",
        ]
        if version == 3
        else []
    )
    assert module.validate_actions(ledger, actions) == expected


def test_consistent_ledger_invariant_history_with_third_occurrence_escalated_is_valid() -> None:
    """A repeated fix with root cause and escalation at the third occurrence remains valid."""
    module = _load_module()
    invariant = "Every case arm is judged as a command"
    ledger = _route_ledger(
        ["a", "pad-1", "pad-2"],
        ["b", "pad-1"],
        ["c"],
        invariants={**dict.fromkeys(("a", "b", "c"), invariant), "pad-1": "padding one", "pad-2": "padding two"},
    )
    actions = _actions(
        [_invariant_action("a"), _held_action("pad-1", "padding one"), _held_action("pad-2", "padding two")],
        [_invariant_action("b", _ROOT_CAUSE), _held_action("pad-1", "padding one")],
        [_held_action("c", invariant)],
        version=3,
    )

    assert module.validate_actions(ledger, actions) == []


@pytest.mark.parametrize("disposition", ["open", "fixed-pending-verification", "verified-fixed", "rejected"])
def test_progress_and_recurrence_share_every_disposition_history(disposition: str) -> None:
    """A closed or rejected first report still makes a later route old and requires root cause for its fix."""
    module = _load_module()
    invariant = "The same boundary holds"
    first = _finding("a", "high", disposition, evidence=[f"invariant: {invariant}", "review evidence"])
    closed = {**first, "disposition": "verified-fixed"}
    ledger = _ledger(
        _round(1, [first, _finding("padding", "security", evidence=["invariant: unrelated"])]),
        _round(
            2,
            [
                closed,
                _finding("padding", "security", "verified-fixed"),
                _finding("b", "high", evidence=[f"invariant: {invariant}"]),
            ],
        ),
    )
    initial_actions = [_held_action("padding", "unrelated")]
    if disposition in {"open", "fixed-pending-verification"}:
        initial_actions.insert(0, _held_action("a", invariant))
    actions = _actions(initial_actions, [_reworded_action("b", invariant)], version=3)

    assert module.validate_ledger(ledger) == []
    assert module.summarize_ledger(ledger)["scores"] == [
        26 if disposition in {"open", "fixed-pending-verification"} else 20,
        6,
    ]
    assert "| 2 | 0 + 0 | 0 + 0 | 1 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 6 + 0 |" in module._render_progress(ledger, 2)
    assert module.validate_actions(ledger, actions) == ["round-2-repeat-root-cause-required:b"]

    actions["rounds"][1]["actions"][0]["root_cause"] = _ROOT_CAUSE
    assert module.validate_actions(ledger, actions) == []


def test_progress_counts_new_route_to_known_invariant_as_old(tmp_path: Path) -> None:
    """The progress table reads a new route to an invariant from an earlier round as an old finding.

    The table keyed old/new by signature only, so a route-named signature for a known invariant showed as new discovery
    while the action check already counted it as a repeat.
    """
    ledger = _route_ledger(
        ["route-one", "pad"],
        ["route-two"],
        invariants={"route-one": "Every case arm is judged", "route-two": "every case arm  is judged", "pad": "pad"},
    )
    ledger_path = tmp_path / "ledger.json"
    # the single-file historical shape keeps the rounds inline, so one JSON write stands in for header plus round log
    ledger_path.write_text(json.dumps(ledger | {"schema_version": 1}), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), "--progress"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "| 1 | 0 + 2 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 40 |" in result.stderr
    assert "| 2 | 1 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 20 + 0 |" in result.stderr


@pytest.mark.parametrize(
    ("decision", "evidence", "expected"),
    [
        pytest.param(
            "escalate", ["Contract change needs an owner."], "action-evidence-missing:invariant:", id="escalate"
        ),
        pytest.param("defer", ["Copy file is out of scope."], "action-evidence-missing:invariant:", id="defer"),
        pytest.param(
            "fix",
            ["original: case c1", "consumer: hook verdict", "sibling: case c2", "source-paths: reader.py"],
            "fix-evidence-missing:invariant:",
            id="fix",
        ),
        pytest.param("escalate", ["invariant:   ", "needs owner"], "action-evidence-missing:invariant:", id="blank"),
    ],
)
def test_current_actions_require_invariant_on_every_decision(decision: str, evidence: list[str], expected: str) -> None:
    """Schema 3 rejects any action, whatever its decision, that does not name a nonempty invariant.

    Without the entry an escalated or deferred finding has no invariant key, and a later route to it slips past the
    repeat and third-occurrence rules.
    """
    module = _load_module()
    ledger = _ledger(_round(1, [_finding("typo", "low")])) | {"schema_version": 2}
    action = {
        "signature": "typo",
        "decision": decision,
        "evidence": evidence,
        "owner": "parent",
        "next_action": "Request the owner decision.",
        "root_cause": None,
    }

    assert module.validate_actions(ledger, _actions([action], version=3)) == [f"round-1-{expected}typo"]


def test_historical_schema_two_actions_keep_their_evidence_rules() -> None:
    """A schema-2 action record without ``invariant:`` on escalated or deferred findings still validates as before.

    Archived schema-2 records were written when only a fix named its invariant; they stay readable under that rule while
    current results require schema 3.
    """
    module = _load_module()
    ledger = _ledger(_round(1, [_finding("boundary"), _finding("typo", "low")])) | {"schema_version": 2}
    held = {"evidence": ["Needs an owner decision."], "owner": "parent", "next_action": "Ask.", "root_cause": None}
    actions = _actions(
        [{**held, "signature": "boundary", "decision": "escalate"}, {**held, "signature": "typo", "decision": "defer"}]
    )

    assert module.validate_actions(ledger, actions) == []


@pytest.mark.parametrize("version", [0, 4, True, "3", 3.0, pytest.param([], id="list"), pytest.param({}, id="dict")])
def test_actions_schema_outside_known_versions_is_rejected(version: object) -> None:
    """Only actions schemas 1, 2 and 3 are accepted; a boolean is not an integer version."""
    module = _load_module()
    ledger = _ledger(_round(1, [_finding("typo", "low")])) | {"schema_version": 2}
    actions = _actions([_held_action("typo", "copy matches the spec", "defer")], version=version)

    assert "actions-schema-version-invalid" in module.validate_actions(ledger, actions)


def test_specialist_retry_policy_permits_corrected_reviewer_redispatch() -> None:
    """The Codex Rig retry policy carries the loop rule's one corrected reviewer re-dispatch.

    The policy otherwise retries only timeout, transport and rate-limit failures, so without this exception a reviewer
    round that returned nothing or a ``blocked`` envelope would never get the re-dispatch the loop rule requires.
    """
    policy = (PLUGIN_ROOT / "shared" / "specialist-orchestration.md").read_text(encoding="utf-8")
    loop_rule = (PLUGIN_ROOT / "shared" / "adversarial-loop.md").read_text(encoding="utf-8")
    assert "gets one corrected re-dispatch with a brief that removes the recorded cause" in policy
    assert "counts toward the 2-attempt cap and is never an identical retry" in policy
    assert "When the cap is already spent, the route is failed" in policy
    assert "Strict portable `parallel_execution` nodes are excluded" in policy
    assert "Re-dispatch that reviewer once with a brief that removes the cause;" in loop_rule
    assert "When no attempt remains, the required approval is not held, or no brief can remove the cause" in loop_rule
