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

    header = "| Iteration | Critical | High | Medium | Low | Nits | Weighted score |"
    first_row = "| 1 | 0 + 2 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 43 |"
    assert baseline.returncode == updated.returncode == 1
    assert header in baseline.stderr
    assert header in updated.stderr
    assert first_row in baseline.stderr
    assert first_row in updated.stderr
    assert "| 2 | 0 + 0 | 1 + 3 | 0 + 0 | 0 + 0 | 0 + 0 | 6 + 18 |" in updated.stderr
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
                        "root_cause": None,
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
