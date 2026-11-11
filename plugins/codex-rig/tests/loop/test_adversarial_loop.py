"""Regression checks for the deterministic adversarial-review convergence ledger.

The standalone ``adversarial_loop.py`` helper is exercised two ways: in process through its validators and summaries
(``validate_ledger``, ``summarize_ledger``, ``validate_actions``, ``plateau_kind``, ``brief_arm``), and through its real
command line, whose exit codes, streams and append-only files are the contract the review-loop skills depend on. A round
log that only serves as arrangement is written directly; the command line stays real wherever it is under test.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import pytest
from _artifact_files import write_json
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = PLUGIN_ROOT / "shared" / "adversarial_loop.py"

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_REMAINING_FINDING_ERROR = "round-after-stop"
_PROGRESS_HEADER = "| Iteration |"
_ROOT_CAUSE = {
    "claim": "Reader default admits what it does not model.",
    "evidence": "Retained source read names the default branch.",
    "falsification": "A refusing default rejects the original case.",
    "rejected_alternative": "Listing the reported route closes only that route.",
}


def _load_module() -> ModuleType:
    """Load the standalone convergence-ledger helper without installation."""
    return load_module("codex_rig_adversarial_loop", LEDGER_PATH)


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


def _action(
    signature: str, decision: str, evidence: list[str], next_action: str, root_cause: dict[str, str] | None = None
) -> dict[str, Any]:
    """Build one parent action record for a finding."""
    return {
        "signature": signature,
        "decision": decision,
        "evidence": evidence,
        "owner": "parent",
        "next_action": next_action,
        "root_cause": root_cause,
    }


def _actions(*rounds: list[dict[str, Any]], version: int = 2) -> dict[str, Any]:
    """Wrap per-round action lists in an actions document, schema 2 unless a version is given."""
    return {"schema_version": version, "rounds": [{"index": i, "actions": a} for i, a in enumerate(rounds, start=1)]}


def _invariant_action(signature: str, root_cause: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a fix action whose evidence names one shared invariant."""
    evidence = [
        "invariant: Every case arm is judged as a command",
        "original: retained case file c1",
        "consumer: hook verdict",
        "sibling: retained case file c2",
        "source-paths: reader.py",
    ]
    return _action(signature, "fix", evidence, "Seek independent verification", root_cause)


def _held_action(signature: str, invariant: str, decision: str = "escalate") -> dict[str, Any]:
    """Build a schema-3 action that does not fix its finding but still names the invariant it violates."""
    evidence = [f"invariant: {invariant}", "Contract change needs an owner decision."]
    return _action(signature, decision, evidence, "Request the owner decision.")


def _reworded_action(signature: str, invariant: str, root_cause: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a schema-3 fix action whose ``invariant:`` evidence carries the given wording."""
    action = _invariant_action(signature, root_cause)
    return {**action, "evidence": [f"invariant: {invariant}", *action["evidence"][1:]]}


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


def _improving_rounds() -> list[dict[str, Any]]:
    """Build five rounds whose open findings shrink from four to none, scoring 40, 20, 10, 4 and 0."""
    tiers = ["security", "critical", "high", "medium"]
    return [
        _round(
            index,
            [
                _finding(f"finding-{number}", tier, "verified-fixed" if number < index else "open")
                for number, tier in enumerate(tiers, start=1)
            ],
        )
        for index in range(1, 6)
    ]


def _plateau_ledger(second: list[dict[str, Any]], first: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a two-round ledger whose second round does not lower the score."""
    opening = first if first is not None else [_finding("finding-a", "medium")]
    return _ledger(_round(1, opening), _round(2, second))


def _metrics_record(**overrides: Any) -> dict[str, Any]:
    """Build a valid current cost record: a token total with no input/output split, as a Claude agent reports it."""
    record: dict[str, Any] = {
        "schema_version": 2,
        "kind": "round",
        "round": 1,
        "route": "agent:foundry:challenger",
        "brief": "full",
        "brief_bytes": 4096,
        "duration_s": 310,
        "tokens_in": None,
        "tokens_out": None,
        "tokens_total": 105891,
        "attested_gaps": 2,
    }
    return record | overrides


def _legacy_metrics_record(**overrides: Any) -> dict[str, Any]:
    """Build a valid schema 1 cost record, which has no ``tokens_total`` field."""
    record = _metrics_record() | {"schema_version": 1}
    del record["tokens_total"]
    return record | overrides


def _write_header(path: Path, digest: str = _DIGEST_A) -> None:
    """Write a current schema-2 ledger header with no completed round."""
    write_json(
        path, {key: value for key, value in _ledger(digest=digest).items() if key != "rounds"} | {"schema_version": 2}
    )


def _seed_rounds(ledger_path: Path, *rounds: dict[str, Any]) -> None:
    """Write completed rounds straight into the round log, the arrangement that precedes a command under test."""
    lines = "".join(json.dumps(record, sort_keys=True) + "\n" for record in rounds)
    ledger_path.with_name("loop-rounds.jsonl").write_text(lines, encoding="utf-8", newline="\n")


def _cli(ledger_path: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """Run the real standalone helper against ``ledger_path`` with the given flags."""
    return subprocess.run(
        [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), *flags],
        capture_output=True,
        text=True,
        check=False,
    )


def _append(ledger_path: Path, round_record: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """Stage one round beside the header and append it through the CLI, as the lifecycle owner does."""
    (ledger_path.parent / "loop-rounds.jsonl.rec").write_text(json.dumps(round_record, indent=2), encoding="utf-8")
    return _cli(ledger_path, "--append", "--progress")


def _append_each(
    ledger_path: Path, rounds: list[dict[str, Any]]
) -> list[tuple[subprocess.CompletedProcess[str], bytes]]:
    """Append each round in order, pairing every CLI result with the round-log bytes it left behind."""
    log = ledger_path.with_name("loop-rounds.jsonl")
    steps = []
    for record in rounds:
        result = _append(ledger_path, record)
        steps.append((result, log.read_bytes()))
    return steps


def _run_metrics(
    ledger_path: Path,
    metrics_record: dict[str, Any] | None,
    round_record: dict[str, Any] | None = None,
    extra_flags: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Stage a cost record (and optionally a round) and append through the CLI in one helper call."""
    flags = ["--append-metrics", *extra_flags]
    if metrics_record is not None:
        (ledger_path.parent / "loop-metrics.jsonl.rec").write_text(json.dumps(metrics_record), encoding="utf-8")
    if round_record is not None:
        (ledger_path.parent / "loop-rounds.jsonl.rec").write_text(json.dumps(round_record), encoding="utf-8")
        flags.insert(0, "--append")
    return _cli(ledger_path, *flags)


def _prose(name: str) -> str:
    """Read a shared Markdown document with whitespace collapsed, so line wrapping cannot affect a phrase check."""
    return " ".join((PLUGIN_ROOT / "shared" / name).read_text(encoding="utf-8").split())


def _missing(text: str, phrases: list[str]) -> list[str]:
    """Return the phrases absent from ``text``, so one assertion names every missing phrase at once."""
    return [phrase for phrase in phrases if phrase not in text]


@pytest.fixture
def loop() -> ModuleType:
    """Load the standalone helper as a module."""
    return _load_module()


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    """Write a current-schema ledger header with no completed round and return its path."""
    path = tmp_path / "loop-ledger.json"
    _write_header(path)
    return path


@pytest.fixture
def rounds_log(tmp_path: Path) -> Path:
    """Return the round log path beside the ledger header."""
    return tmp_path / "loop-rounds.jsonl"


@pytest.fixture
def rounds_rec(tmp_path: Path) -> Path:
    """Return the staged round record path beside the ledger header."""
    return tmp_path / "loop-rounds.jsonl.rec"


@pytest.fixture
def metrics_log(tmp_path: Path) -> Path:
    """Return the cost log path beside the ledger header."""
    return tmp_path / "loop-metrics.jsonl"


@pytest.fixture
def metrics_rec(tmp_path: Path) -> Path:
    """Return the staged cost record path beside the ledger header."""
    return tmp_path / "loop-metrics.jsonl.rec"


class TestModuleSurface:
    """The helper's import contract."""

    def test_validator_is_importable(self, loop: ModuleType) -> None:
        """The standalone helper exposes ``validate_ledger`` for skill lifecycle owners.

        Skills load the file by path, with no installed package, so the validator must be reachable on the bare module.
        """
        assert callable(loop.validate_ledger)


class TestSummarizeLedger:
    """``summarize_ledger`` decisions: baseline, clean, converging, plateau, non-converging and stops."""

    def test_initial_review_is_baseline_and_pending_counts_open(self, loop: ModuleType) -> None:
        """Keep the initial W_0 and pending-verification weight in the score.

        A security finding marked fixed but not yet independently verified still weighs 20, so the first round is a
        baseline rather than a clean pass.
        """
        ledger = _ledger(_round(1, [_finding("finding-a", "security", "fixed-pending-verification")]))

        assert loop.validate_ledger(ledger) == []
        assert loop.summarize_ledger(ledger) == {
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

    @pytest.mark.parametrize(
        ("ledger", "reason"),
        [
            pytest.param(
                _ledger(_round(1, [_finding("finding-a", disposition="verified-fixed")], _DIGEST_A), digest=_DIGEST_B),
                "stale-review",
                id="reviewed-an-older-snapshot",
            ),
            pytest.param(
                _ledger(_round(1, [_finding("finding-a", disposition="verified-fixed")])),
                "clean",
                id="reviewed-current",
            ),
        ],
    )
    def test_clean_requires_independent_review_of_current_snapshot(
        self, loop: ModuleType, ledger: dict[str, Any], reason: str
    ) -> None:
        """Block a false clean result from an old reviewed revision.

        Both ledgers close every finding; only the one whose review covers the current diff digest may be called clean.
        """
        assert loop.summarize_ledger(ledger)["reason"] == reason

    @pytest.mark.parametrize(
        "ledger",
        [
            pytest.param(
                _ledger(
                    _round(1, [_finding("security-a", "security")]),
                    _round(
                        2, [_finding("security-a", "security", "verified-fixed"), _finding("critical-b", "critical")]
                    ),
                ),
                id="security-replaced-by-critical",
            ),
            pytest.param(
                _ledger(
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
                ),
                id="smaller-decrease",
            ),
            pytest.param(
                _ledger(
                    _round(1, [_finding("security-a", "security")]),
                    _round(
                        2, [_finding("security-a", "security", "verified-fixed"), _finding("critical-b", "critical")]
                    ),
                    _round(
                        3,
                        [
                            _finding("security-a", "security", "verified-fixed"),
                            _finding("critical-b", "critical", "verified-fixed"),
                            _finding("medium-c", "medium"),
                        ],
                    ),
                ),
                id="three-improving-rounds",
            ),
        ],
    )
    def test_score_decrease_stays_converging(self, loop: ModuleType, ledger: dict[str, Any]) -> None:
        """Continue after any decrease without a fixed review-round cap.

        The score only has to fall from one review to the next; a small fall and a third round are both still
        converging.
        """
        assert loop.summarize_ledger(ledger)["reason"] == "converging"

    @pytest.mark.parametrize(
        ("tier", "reason"),
        [
            pytest.param("high", "plateau", id="unchanged-score"),
            pytest.param("critical", "nonconverging", id="increased-score"),
        ],
    )
    def test_unchanged_or_increased_score_stops(self, loop: ModuleType, tier: str, reason: str) -> None:
        """Stop true score plateaus and regressions without confusing a smaller decrease.

        Round 2 fixes the one high finding but reports a new one: the same tier keeps the score level, a higher tier
        raises it.
        """
        ledger = _ledger(
            _round(1, [_finding("initial", "high")]),
            _round(2, [_finding("initial", "high", "verified-fixed"), _finding("remaining", tier)]),
        )

        assert loop.summarize_ledger(ledger)["reason"] == reason

    @pytest.mark.parametrize(
        ("tier", "reason"),
        [
            pytest.param("medium", "plateau", id="late-plateau"),
            pytest.param("high", "nonconverging", id="late-increase"),
        ],
    )
    def test_late_nonimprovement_stops_and_refuses_a_further_round(
        self, loop: ModuleType, tier: str, reason: str
    ) -> None:
        """Removing the cardinality limit never permits extension after a late score stop.

        Three improving rounds are followed by a fourth that stops converging; a fifth round that closes everything is
        still refused because the ledger already ended.
        """
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
        closed = [dict(item, disposition="verified-fixed", evidence=["verified"]) for item in latest]
        continued = {**stopped, "rounds": [*stopped["rounds"], _round(5, closed)]}

        assert loop.validate_ledger(stopped) == []
        assert loop.summarize_ledger(stopped)["reason"] == reason
        assert _REMAINING_FINDING_ERROR in loop.validate_ledger(continued)

    @pytest.mark.parametrize(
        ("ledger", "reason"),
        [
            pytest.param(
                _ledger(_round(1, [_finding("contract-change", structural=True)])), "baseline", id="structural"
            ),
            pytest.param(
                _ledger(
                    _round(1, [_finding("finding-a"), _finding("finding-b")]),
                    _round(2, [_finding("finding-a"), _finding("finding-b", disposition="verified-fixed")]),
                ),
                "converging",
                id="repeated",
            ),
        ],
    )
    def test_feasible_structural_and_repeated_findings_can_converge(
        self, loop: ModuleType, ledger: dict[str, Any], reason: str
    ) -> None:
        """Keep authorized structural and repeated findings eligible for another challenge.

        Neither a structural flag nor a signature that stays open across rounds is a stop by itself while the score
        still falls.
        """
        assert loop.validate_ledger(ledger) == []
        assert loop.summarize_ledger(ledger)["reason"] == reason

    @pytest.mark.parametrize(
        ("disposition", "decision", "score"),
        [
            pytest.param("rejected", "clean", 0, id="refuted-structural-finding"),
            pytest.param("verified-fixed", "clean", 0, id="verified-structural-carryover"),
            pytest.param("open", "baseline", 6, id="open-structural-finding"),
            pytest.param("fixed-pending-verification", "baseline", 6, id="unverified-structural-fix"),
        ],
    )
    def test_structural_stop_respects_verified_disposition(
        self, loop: ModuleType, disposition: str, decision: str, score: int
    ) -> None:
        """Retain closed structural evidence without blocking a separately approved correction's clean review.

        A structural finding only keeps the ledger open while it is unresolved; once refuted or verified fixed it adds
        no weight.
        """
        ledger = _ledger(_round(1, [_finding("contract-change", disposition=disposition, structural=True)]))

        summary = loop.summarize_ledger(ledger)

        assert summary["reason"] == decision
        assert summary["scores"] == [score]

    @pytest.mark.parametrize(
        "ledger",
        [
            pytest.param(_ledger(_round(1, [_finding("finding-a")], independent=False)), id="unavailable-baseline"),
            pytest.param(_ledger(), id="no-round-at-all"),
        ],
    )
    def test_missing_independent_coverage_is_a_valid_stop(self, loop: ModuleType, ledger: dict[str, Any]) -> None:
        """Preserve an unavailable reviewer route as a reportable non-clean outcome.

        A ledger with no independent review is not malformed; it stops with ``independence-unavailable`` so the caller
        can report the gap.
        """
        assert loop.validate_ledger(ledger) == []
        assert loop.summarize_ledger(ledger)["reason"] == "independence-unavailable"

    def test_unavailable_second_review_is_terminal_but_not_a_shape_error(self, loop: ModuleType) -> None:
        """Retain the last independent score before an unavailable reviewer route stops work.

        Round 2 has no independent reviewer; the ledger is still well formed and reports the stop.
        """
        stopped = _ledger(_round(1, [_finding("finding-a")]), _round(2, [_finding("finding-a")], independent=False))

        assert loop.validate_ledger(stopped) == []
        assert loop.summarize_ledger(stopped)["reason"] == "independence-unavailable"

    def test_round_after_unavailable_review_is_refused(self, loop: ModuleType) -> None:
        """Refuse a third round recorded after the reviewer route became unavailable.

        The stop is terminal, so the ledger cannot be extended past it.
        """
        continued = _ledger(
            _round(1, [_finding("finding-a")]),
            _round(2, [_finding("finding-a")], independent=False),
            _round(3, [_finding("finding-a")]),
        )

        assert _REMAINING_FINDING_ERROR in loop.validate_ledger(continued)


class TestValidateLedger:
    """``validate_ledger`` shape, history and value checks."""

    @pytest.mark.parametrize("later_disposition", ["open", "verified-fixed"])
    def test_rejects_a_round_after_a_terminal_decision(self, loop: ModuleType, later_disposition: str) -> None:
        """Require a separately scoped run instead of extending a stopped ledger.

        Round 1 already ends on a verified structural finding, so any later round, open or closed, is refused.
        """
        continued = _ledger(
            _round(1, [_finding("contract-change", disposition="verified-fixed", structural=True)]),
            _round(2, [_finding("contract-change", disposition=later_disposition, structural=True)]),
        )

        assert _REMAINING_FINDING_ERROR in loop.validate_ledger(continued)

    @pytest.mark.parametrize(
        ("ledger", "errors"),
        [
            pytest.param(
                _ledger(
                    _round(1, [_finding("finding-a", disposition="verified-fixed"), _finding("finding-b", "medium")]),
                    _round(2, [_finding("finding-a"), _finding("finding-b", "medium", "verified-fixed")]),
                ),
                [],
                id="terminal-finding-reopens",
            ),
            pytest.param(
                _ledger(_round(1, [_finding("finding-a")]), _round(2, [])),
                ["round-2-finding-dropped:finding-a"],
                id="dropped-signature",
            ),
            pytest.param(
                _ledger(_round(1, [_finding("finding-a", "high")]), _round(2, [_finding("finding-a", "low")])),
                ["round-2-finding-tier-changed:finding-a"],
                id="tier-changed",
            ),
        ],
    )
    def test_history_is_stable_but_a_terminal_finding_can_reopen(
        self, loop: ModuleType, ledger: dict[str, Any], errors: list[str]
    ) -> None:
        """Require carried signatures while allowing later evidence of a real regression.

        A later round must repeat every earlier signature at its original tier, but a finding that was verified fixed
        may come back open.
        """
        assert loop.validate_ledger(ledger) == errors

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
    def test_rejects_false_green_shape_and_value_claims(
        self, loop: ModuleType, mutate: Any, expected_error: str
    ) -> None:
        """Reject bools, self-review, malformed snapshots, and unsupported finding claims.

        Each mutation corrupts one valid ledger; the validator must name the error and ``summarize_ledger`` must refuse
        to score it rather than report a green result.
        """
        ledger = _ledger(_round(1, [_finding("finding-a")]))
        mutate(ledger)

        assert expected_error in loop.validate_ledger(ledger)
        with pytest.raises(ValueError, match=expected_error):
            loop.summarize_ledger(ledger)


class TestCliSummary:
    """The standalone command line's JSON summary, exit codes and stream separation."""

    def test_valid_ledger_prints_summary_on_stdout(self, tmp_path: Path) -> None:
        """A valid ledger prints its JSON summary on stdout, exits 0 and leaves stderr empty.

        Skills parse stdout as JSON, so the success path must not mix anything else into either stream.
        """
        valid_path = tmp_path / "valid.json"
        write_json(valid_path, _ledger(_round(1, [_finding("finding-a")])))

        valid = _cli(valid_path)

        assert valid.returncode == 0
        assert json.loads(valid.stdout)["reason"] == "baseline"
        assert valid.stderr == ""

    def test_invalid_ledger_keeps_output_off_stdout(self, tmp_path: Path) -> None:
        """An invalid ledger exits 1 with its error summary on stderr only.

        A gate that reads stdout must never see an invalid-ledger payload that looks like a result.
        """
        invalid_path = tmp_path / "invalid.json"
        write_json(invalid_path, {"schema_version": True})

        invalid = _cli(invalid_path, "--progress")

        assert invalid.returncode == 1
        assert invalid.stdout == ""
        assert json.loads(invalid.stderr)["reason"] == "invalid-ledger"

    @pytest.mark.parametrize(
        ("ledger", "flags", "returncode", "reason"),
        [
            pytest.param(
                _ledger(_round(1, [_finding("finding-a", disposition="verified-fixed")])),
                ("--require-clean",),
                0,
                "clean",
                id="clean-passes-the-gate",
            ),
            pytest.param(_ledger(), (), 0, "independence-unavailable", id="blocked-without-the-gate"),
            pytest.param(_ledger(), ("--require-clean",), 1, "independence-unavailable", id="blocked-fails-the-gate"),
        ],
    )
    def test_require_clean_fails_closed_but_preserves_valid_summary(
        self, tmp_path: Path, ledger: dict[str, Any], flags: tuple[str, ...], returncode: int, reason: str
    ) -> None:
        """Let a review gate reject valid-but-nonclean ledgers without hiding their result.

        ``--require-clean`` only changes the exit status: the JSON summary is still printed, and only a clean ledger
        exits 0 under the flag.
        """
        ledger_path = tmp_path / "ledger.json"
        write_json(
            ledger_path, {key: value for key, value in ledger.items() if key != "rounds"} | {"schema_version": 2}
        )
        ledger_path.with_name("rounds.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in ledger["rounds"]), encoding="utf-8", newline="\n"
        )

        result = _cli(ledger_path, *flags)

        assert result.returncode == returncode
        assert json.loads(result.stdout)["reason"] == reason
        assert result.stderr == ""

    def test_current_header_with_inline_rounds_is_unreadable(self, tmp_path: Path) -> None:
        """Reject a schema-2 header that also carries rounds, because two copies of history could disagree."""
        ledger_path = tmp_path / "loop-ledger.json"
        write_json(ledger_path, _ledger() | {"schema_version": 2})

        result = _cli(ledger_path)

        assert result.returncode == 1
        assert json.loads(result.stderr)["reason"] == "ledger-read-failed"
        assert "ledger-header-inline-rounds-forbidden" in result.stderr


class TestCliProgress:
    """The ``--progress`` table printed on stderr."""

    def test_reprints_history_and_splits_old_new_weights(self, loop: ModuleType, tmp_path: Path) -> None:
        """Append a reviewed row without changing earlier rows, JSON, or stop decisions.

        The first run records a six-tier baseline; the second adds a round that fixes most findings and reports three
        new ones. The table must reprint round 1 unchanged and split round 2 into old and new weight.
        """
        ledger_path = tmp_path / "ledger.json"
        first = [_finding(tier, tier) for tier in ("security", "critical", "high", "medium", "low", "nit")]
        ledger = _ledger(_round(1, first))
        write_json(ledger_path, ledger)
        baseline = _cli(ledger_path, "--progress", "--require-clean")
        fixed = [
            _finding(
                item["signature"],
                item["tier"],
                "fixed-pending-verification" if item["tier"] == "high" else "verified-fixed",
            )
            for item in first
        ]
        ledger["rounds"].append(_round(2, fixed + [_finding(f"new-{index}") for index in range(3)]))
        write_json(ledger_path, ledger)

        updated = _cli(ledger_path, "--progress", "--require-clean")

        header = "| Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score |"
        first_row = "| 1 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 1 | 0 + 43 |"
        assert baseline.returncode == updated.returncode == 1
        assert header in baseline.stderr
        assert header in updated.stderr
        assert first_row in baseline.stderr
        assert first_row in updated.stderr
        assert "| 2 | 0 + 0 | 0 + 0 | 1 + 3 | 0 + 0 | 0 + 0 | 0 + 0 | 6 + 18 |" in updated.stderr
        assert json.loads(baseline.stdout) == loop.summarize_ledger(_ledger(_round(1, first)))
        assert json.loads(updated.stdout) == loop.summarize_ledger(ledger)
        assert json.loads(updated.stdout)["reason"] == "converging"

    def test_counts_reopened_signatures_as_old(self, tmp_path: Path) -> None:
        """Use signature history rather than just the previous round's open findings.

        ``prior`` is rejected in round 1 and reopened in round 2, so it counts as old weight in round 2 even though it
        was not open in round 1.
        """
        ledger = _ledger(
            _round(1, [_finding("prior", disposition="rejected"), _finding("initial", "security")]),
            _round(2, [_finding("prior"), _finding("initial", "security", "verified-fixed"), _finding("fresh", "nit")]),
        )
        ledger_path = tmp_path / "ledger.json"
        write_json(ledger_path, ledger)

        result = _cli(ledger_path, "--progress")

        assert result.returncode == 0
        assert "| 1 | 0 + 1 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 20 |" in result.stderr
        assert "| 2 | 0 + 0 | 0 + 0 | 1 + 0 | 0 + 0 | 0 + 0 | 0 + 1 | 6 + 1 |" in result.stderr

    def test_new_route_to_known_invariant_counts_as_old(self, tmp_path: Path) -> None:
        """The progress table reads a new route to an invariant from an earlier round as an old finding.

        The table keyed old/new by signature only, so a route-named signature for a known invariant showed as new
        discovery while the action check already counted it as a repeat.
        """
        ledger = _route_ledger(
            ["route-one", "pad"],
            ["route-two"],
            invariants={
                "route-one": "Every case arm is judged",
                "route-two": "every case arm  is judged",
                "pad": "pad",
            },
        )
        ledger_path = tmp_path / "ledger.json"
        # the single-file historical shape keeps rounds inline, so one JSON write stands in for header plus round log
        write_json(ledger_path, ledger | {"schema_version": 1})

        result = _cli(ledger_path, "--progress")

        assert result.returncode == 0, result.stderr
        assert "| 1 | 0 + 2 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 40 |" in result.stderr
        assert "| 2 | 1 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 20 + 0 |" in result.stderr

    @pytest.mark.parametrize("independent", [True, False])
    def test_omits_table_before_any_completed_review(self, tmp_path: Path, independent: bool) -> None:
        """Keep unavailable review status factual without a placeholder table.

        With no round at all, or only an unavailable-reviewer round, nothing was completed, so stderr stays empty.
        """
        ledger = _ledger() if independent else _ledger(_round(1, [], independent=False))
        ledger_path = tmp_path / "ledger.json"
        write_json(ledger_path, ledger)

        result = _cli(ledger_path, "--progress")

        assert result.returncode == 0
        assert result.stderr == ""
        assert json.loads(result.stdout)["reason"] == "independence-unavailable"

    @pytest.mark.parametrize(
        ("independent", "returncode", "shows_second_row", "reason"),
        [
            pytest.param(True, 0, True, "clean", id="independent-second-review"),
            pytest.param(False, 1, False, "independence-unavailable", id="unavailable-second-review"),
        ],
    )
    def test_only_appends_completed_reviews(
        self, tmp_path: Path, independent: bool, returncode: int, shows_second_row: bool, reason: str
    ) -> None:
        """Show a clean zero row only for independent coverage, retaining prior history either way.

        Round 2 closes the one finding; its zero row appears only when the reviewer was independent.
        """
        ledger = _ledger(
            _round(1, [_finding("prior")]),
            _round(2, [_finding("prior", disposition="verified-fixed")], independent=independent),
        )
        ledger_path = tmp_path / "ledger.json"
        write_json(
            ledger_path, {key: value for key, value in ledger.items() if key != "rounds"} | {"schema_version": 2}
        )
        ledger_path.with_name("rounds.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in ledger["rounds"]), encoding="utf-8", newline="\n"
        )

        result = _cli(ledger_path, "--progress", "--require-clean")

        assert result.returncode == returncode
        assert "| 1 | 0 + 0 | 0 + 0 | 0 + 1 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 6 |" in result.stderr
        assert ("| 2 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 |" in result.stderr) is shows_second_row
        assert json.loads(result.stdout)["reason"] == reason

    def test_prints_nothing_for_an_empty_history(self, tmp_path: Path) -> None:
        """An empty round history prints no table and no legend."""
        ledger_path = tmp_path / "ledger.json"
        write_json(ledger_path, _ledger())

        result = _cli(ledger_path, "--progress")

        assert result.returncode == 0
        assert result.stderr == ""

    @pytest.mark.parametrize("completed_reviews", [1, 2])
    def test_separates_legend_from_markdown_table(self, tmp_path: Path, completed_reviews: int) -> None:
        """Keep the legend out of the rendered table for cumulative histories.

        A blank line must separate the Markdown table from the legend, or the legend renders as a table row.
        """
        rounds = [_round(1, [_finding("prior")]), _round(2, [_finding("prior", disposition="verified-fixed")])]
        ledger_path = tmp_path / "ledger.json"
        write_json(ledger_path, _ledger(*rounds[:completed_reviews]))

        result = _cli(ledger_path, "--progress")

        table, separator, legend = result.stderr.partition("\n\n")
        assert result.returncode == 0
        assert separator == "\n\n"
        assert len(table.splitlines()) == 2 + completed_reviews
        assert all(line.startswith("| ") and line.endswith(" |") for line in table.splitlines())
        assert legend.startswith("Cells: old + new open findings")
        assert "Cells:" not in table


class TestAppendRound:
    """``--append`` grows the round log one staged round at a time."""

    def test_round_trips_rounds_without_rewriting_earlier_lines(
        self, loop: ModuleType, ledger_path: Path, rounds_log: Path, rounds_rec: Path
    ) -> None:
        """Grow the round log one staged round at a time and read it back as the assembled ledger.

        The second append must leave the first round's line byte-identical: the log is only appended, so a dropped or
        reworded earlier round can never be introduced by recording a later one.
        """
        first = _round(1, [_finding("finding-a")])
        second = _round(2, [_finding("finding-a", disposition="verified-fixed")])

        baseline = _append(ledger_path, first)
        first_line = rounds_log.read_bytes()
        final = _append(ledger_path, second)

        log = rounds_log.read_bytes()
        assert baseline.returncode == 0, baseline.stderr
        assert final.returncode == 0, final.stderr
        assert log.startswith(first_line)
        assert len(log.splitlines()) == 2
        assert not rounds_rec.exists()
        assert loop.load_ledger(ledger_path) == _ledger(first, second) | {"schema_version": 2}
        assert json.loads(final.stdout) == loop.summarize_ledger(_ledger(first, second))
        assert "| 2 | 0 + 0 |" in final.stderr

    @pytest.mark.parametrize(
        ("staged", "expected_error"),
        [
            pytest.param(
                _round(3, [_finding("finding-a")]), "round-index-must-be-contiguous-integer", id="skipped-index"
            ),
            pytest.param(_round(1, []), "round-index-must-be-contiguous-integer", id="repeated-index"),
            pytest.param(_round(2, []), "round-2-finding-dropped:finding-a", id="dropped-signature"),
        ],
    )
    def test_refuses_invalid_round_and_keeps_staged_record(
        self, ledger_path: Path, rounds_log: Path, rounds_rec: Path, staged: dict[str, Any], expected_error: str
    ) -> None:
        """Reject a staged round that would break history, appending nothing and keeping the record for repair.

        The ledger already holds round 1; the staged round skips an index, repeats one, or drops a signature.
        """
        _seed_rounds(ledger_path, _round(1, [_finding("finding-a")]))
        before = rounds_log.read_bytes()

        rejected = _append(ledger_path, staged)

        assert rejected.returncode == 1
        assert rejected.stdout == ""
        assert json.loads(rejected.stderr)["reason"] == "append-rejected"
        assert expected_error in json.loads(rejected.stderr)["errors"]
        assert rounds_log.read_bytes() == before
        assert rounds_rec.is_file()

    def test_baseline_round_needs_no_origin_entry(self, ledger_path: Path) -> None:
        """The baseline round names no origin class and draws no origin warning.

        Origins only apply to findings first reported after the baseline.
        """
        baseline = _append(ledger_path, _round(1, [_finding("finding-a")]))

        assert baseline.returncode == 0
        assert "origin-missing" not in baseline.stderr

    def test_warns_when_a_later_new_finding_lacks_an_origin_entry(self, ledger_path: Path, rounds_log: Path) -> None:
        """Nudge, never refuse, when a finding first reported after the baseline names no origin class.

        The loop rule already requires the origin; the warning makes a missing one visible at the moment it is cheap to
        add. Findings with a recognised origin class, in any letter case and with trailing text, draw no warning.
        """
        _seed_rounds(ledger_path, _round(1, [_finding("finding-a")]))

        later = _append(
            ledger_path,
            _round(
                2,
                [
                    _finding("finding-a", disposition="verified-fixed"),
                    _finding("finding-b", "medium"),
                    _finding("finding-c", "medium", evidence=["origin: resolver-edit"]),
                    _finding("finding-f", "medium", evidence=["origin: Resolver-Edit (round 2 diff)"]),
                    _finding("finding-d", "medium", evidence=["origin:"]),
                    _finding("finding-e", "medium", evidence=["origin: banana"]),
                ],
            ),
        )

        warned = sorted(line for line in later.stderr.splitlines() if "origin-missing" in line)
        assert later.returncode == 0, later.stderr
        assert warned == [
            "warning: round-2-finding-origin-missing:finding-b",
            "warning: round-2-finding-origin-missing:finding-d",
            "warning: round-2-finding-origin-missing:finding-e",
        ]
        assert len(rounds_log.read_bytes().splitlines()) == 2

    def test_warning_escapes_a_newline_in_a_signature(self, ledger_path: Path) -> None:
        """Escape newlines in a signature so record text cannot forge extra warning lines."""
        _seed_rounds(ledger_path, _round(1, [_finding("finding-a")]))

        appended = _append(
            ledger_path,
            _round(2, [_finding("finding-a", disposition="verified-fixed"), _finding("sig\nsecond-line", "medium")]),
        )

        assert appended.returncode == 0, appended.stderr
        assert "warning: round-2-finding-origin-missing:sig\\nsecond-line" in appended.stderr
        assert "second-line" not in [line.strip() for line in appended.stderr.splitlines()]


class TestAppendMetrics:
    """``--append-metrics`` records an advisory cost line per completed round."""

    def test_records_cost_beside_the_round_without_changing_the_ledger(
        self, loop: ModuleType, ledger_path: Path, rounds_log: Path, metrics_log: Path, metrics_rec: Path
    ) -> None:
        """Record a round and its cost in one helper call, leaving the ledger summary identical to a round-only append.

        The cost log is advisory data for later analysis, so adding it must never change the score or the round log.
        """
        first = _round(1, [_finding("finding-a")])

        result = _run_metrics(ledger_path, _metrics_record(), first)

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == loop.summarize_ledger(_ledger(first) | {"schema_version": 2})
        assert json.loads(metrics_log.read_text(encoding="utf-8")) == _metrics_record()
        assert len(rounds_log.read_bytes().splitlines()) == 1
        assert not metrics_rec.exists()

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param(_legacy_metrics_record(), id="schema-one-without-token-total"),
            pytest.param(_metrics_record(duration_s=312.7), id="fractional-seconds"),
        ],
    )
    def test_accepts_valid_record(self, ledger_path: Path, metrics_log: Path, record: dict[str, Any]) -> None:
        """Keep recording a schema 1 record and a fractional duration exactly as the writer reported them.

        The log is read as one file, so old and new records must sit in it together, and a wall-clock duration needs no
        rounding by callers.
        """
        _seed_rounds(ledger_path, _round(1, [_finding("finding-a")]))

        result = _run_metrics(ledger_path, record)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert json.loads(metrics_log.read_text(encoding="utf-8")) == record

    @pytest.mark.parametrize(
        ("record", "expected_error"),
        [
            pytest.param(
                {k: v for k, v in _metrics_record().items() if k != "tokens_total"},
                "metrics-missing-fields:tokens_total",
                id="current-without-total",
            ),
            pytest.param(_metrics_record(round=2), "metrics-round-must-be-a-completed-round", id="round-not-completed"),
            pytest.param(_metrics_record(round=True), "metrics-round-must-be-a-completed-round", id="round-is-boolean"),
            pytest.param(_metrics_record(schema_version=3), "metrics-schema-version-unsupported", id="unknown-version"),
            pytest.param(
                _metrics_record(tokens_total=-1),
                "metrics-tokens_total-must-be-nonnegative-integer-or-null",
                id="negative-total",
            ),
            pytest.param(
                _legacy_metrics_record() | {"tokens_total": 5},
                "metrics-unexpected-fields:tokens_total",
                id="legacy-with-total",
            ),
            pytest.param(
                _metrics_record(schema_version=True), "metrics-schema-version-unsupported", id="boolean-version"
            ),
            pytest.param(_metrics_record(schema_version=1.0), "metrics-schema-version-unsupported", id="float-version"),
            pytest.param(_metrics_record(kind="origin"), "metrics-kind-invalid", id="unknown-kind"),
            pytest.param(_metrics_record(route=" "), "metrics-route-required", id="blank-route"),
            pytest.param(_metrics_record(brief="short"), "metrics-brief-invalid", id="unknown-brief"),
            pytest.param(_metrics_record(brief=None), "metrics-brief-invalid", id="missing-brief"),
            pytest.param(
                _metrics_record(tokens_in=-1),
                "metrics-tokens_in-must-be-nonnegative-integer-or-null",
                id="negative-count",
            ),
            pytest.param(
                _metrics_record(duration_s=-1),
                "metrics-duration_s-must-be-nonnegative-number-or-null",
                id="negative-time",
            ),
            pytest.param(
                _metrics_record(duration_s=float("nan")),
                "metrics-duration_s-must-be-nonnegative-number-or-null",
                id="not-a-number-time",
            ),
            pytest.param(
                _metrics_record(tokens_in=1.5),
                "metrics-tokens_in-must-be-nonnegative-integer-or-null",
                id="fractional-count",
            ),
            pytest.param(
                _metrics_record(attested_gaps=True),
                "metrics-attested_gaps-must-be-nonnegative-integer-or-null",
                id="boolean-count",
            ),
            pytest.param(_metrics_record() | {"extra": 1}, "metrics-unexpected-fields:extra", id="unexpected-field"),
        ],
    )
    def test_warns_on_invalid_record_and_keeps_staged_record(
        self, ledger_path: Path, metrics_log: Path, metrics_rec: Path, record: dict[str, Any], expected_error: str
    ) -> None:
        """Warn about a cost record that is not a well-formed report on a completed round, appending nothing.

        The record is advisory, so the rejection leaves the exit status and the summary alone.
        """
        _seed_rounds(ledger_path, _round(1, [_finding("finding-a")]))

        rejected = _run_metrics(ledger_path, record)

        assert rejected.returncode == 0, rejected.stderr
        assert f"warning: metrics-rejected:{expected_error}" in rejected.stderr.replace(
            ",", "\nwarning: metrics-rejected:"
        )
        assert json.loads(rejected.stdout)["status"] == "active"
        assert not metrics_log.exists()
        assert metrics_rec.is_file()

    def test_rejected_cost_record_keeps_the_committed_round_and_its_table(
        self, ledger_path: Path, rounds_log: Path, rounds_rec: Path, metrics_rec: Path
    ) -> None:
        """Deliver a validated round's summary and progress table even when its advisory cost record is bad.

        The round is committed and its staging file consumed before the cost record is judged, so a failure there must
        not turn the call into an error that makes a retry hit a missing staged round.
        """
        result = _run_metrics(
            ledger_path, _metrics_record(tokens_in=-1), _round(1, [_finding("finding-a")]), extra_flags=("--progress",)
        )

        assert result.returncode == 0, result.stderr
        assert "warning: metrics-rejected:metrics-tokens_in-must-be-nonnegative-integer-or-null" in result.stderr
        assert _PROGRESS_HEADER in result.stderr
        assert json.loads(result.stdout)["rounds"][0]["index"] == 1
        assert len(rounds_log.read_bytes().splitlines()) == 1
        assert not rounds_rec.exists()
        assert metrics_rec.is_file()

    @pytest.mark.parametrize(
        ("staged_text", "warns"),
        [
            pytest.param(
                json.dumps(_metrics_record()).replace('"duration_s": 310', '"duration_s": ' + "9" * 400),
                False,
                id="int-past-float",
            ),
            pytest.param(
                json.dumps(_metrics_record()).replace('"duration_s": 310', '"duration_s": ' + "9" * 5000),
                True,
                id="int-past-digit-limit",
            ),
            pytest.param("[" * 200000, True, id="absurd-nesting"),
        ],
    )
    def test_pathological_cost_record_never_aborts_a_committed_round_call(
        self, ledger_path: Path, rounds_rec: Path, metrics_rec: Path, staged_text: str, warns: bool
    ) -> None:
        """Contain every way an advisory record can fail, not just the ones it reports as codes.

        Numbers or nesting no honest route writes raise inside parsing or validation; the round is already committed by
        then, so the call must still deliver its summary and table with exit status zero.
        """
        metrics_rec.write_text(staged_text, encoding="utf-8")

        result = _run_metrics(ledger_path, None, _round(1, [_finding("finding-a")]), extra_flags=("--progress",))

        assert result.returncode == 0, result.stderr[-300:]
        assert "Traceback" not in result.stderr
        assert ("warning: metrics-rejected:" in result.stderr) is warns
        assert _PROGRESS_HEADER in result.stderr
        assert json.loads(result.stdout)["rounds"][0]["index"] == 1
        assert not rounds_rec.exists()

    def test_unwritable_cost_log_never_aborts_a_committed_round_call(
        self, ledger_path: Path, metrics_log: Path, metrics_rec: Path
    ) -> None:
        """Keep a locked, read-only or occupied cost-log destination from undoing the round's delivery."""
        metrics_log.mkdir()

        result = _run_metrics(
            ledger_path, _metrics_record(), _round(1, [_finding("finding-a")]), extra_flags=("--progress",)
        )

        assert result.returncode == 0, result.stderr[-300:]
        assert "warning: metrics-rejected:append-metrics-write-failed:" in result.stderr
        assert _PROGRESS_HEADER in result.stderr
        assert metrics_rec.is_file()

    @pytest.mark.parametrize(
        ("record", "expected_warning"),
        [
            pytest.param(
                _metrics_record(route="bridge:re\nview"),
                "warning: metrics-route-not-in-vocabulary:'bridge:re\\nview'",
                id="newline-in-route",
            ),
            pytest.param(
                _metrics_record() | {"x\ny": 1},
                "warning: metrics-rejected:metrics-unexpected-fields:x\\ny",
                id="newline-in-unexpected-key",
            ),
        ],
    )
    def test_warning_stays_on_one_line(self, ledger_path: Path, record: dict[str, Any], expected_warning: str) -> None:
        """Keep each stderr warning to one line even when record text carries an interior newline.

        An unescaped newline in a route or an unexpected key would let record text forge extra warning lines.
        """
        _seed_rounds(ledger_path, _round(1, [_finding("finding-a")]))

        result = _run_metrics(ledger_path, record)

        assert result.returncode == 0, result.stderr
        assert result.stderr.splitlines() == [expected_warning]

    def test_reports_a_missing_staged_record(self, ledger_path: Path, metrics_log: Path) -> None:
        """Name the absent staged file instead of appending an empty or stale cost record."""
        rejected = _run_metrics(ledger_path, None)

        assert rejected.returncode == 0
        assert "warning: metrics-rejected:append-metrics-record-missing:loop-metrics.jsonl.rec" in rejected.stderr
        assert not metrics_log.exists()

    @pytest.mark.parametrize(
        ("header", "expected_error"),
        [
            pytest.param([], "append-metrics-requires-current-schema", id="header-is-a-list"),
            pytest.param({"schema_version": 3}, "append-metrics-requires-current-schema", id="other-schema"),
        ],
    )
    def test_refuses_a_ledger_it_cannot_bind_to(
        self, tmp_path: Path, metrics_log: Path, metrics_rec: Path, header: Any, expected_error: str
    ) -> None:
        """Name a stable error, never a traceback, for a header that is not the current ledger shape."""
        ledger_path = tmp_path / "loop-ledger.json"
        write_json(ledger_path, header)

        result = _run_metrics(ledger_path, _metrics_record())

        assert "Traceback" not in result.stderr
        assert expected_error in result.stderr or "ledger-read-failed" in result.stderr
        assert not metrics_log.exists()
        assert metrics_rec.is_file()

    def test_refuses_an_invalid_round_log_without_writing(
        self, ledger_path: Path, rounds_log: Path, metrics_log: Path, metrics_rec: Path
    ) -> None:
        """Bind cost only to rounds the ledger validator accepts, not to raw lines of a broken log."""
        rounds_log.write_text(json.dumps({"index": 1}) + "\n", encoding="utf-8")

        result = _run_metrics(ledger_path, _metrics_record())

        assert "warning: metrics-rejected:" in result.stderr
        assert not metrics_log.exists()
        assert metrics_rec.is_file()

    def test_refuses_the_historical_inline_schema(self, tmp_path: Path, metrics_log: Path) -> None:
        """Keep a frozen schema-1 archive free of new files, as the round append already does."""
        ledger_path = tmp_path / "loop-ledger.json"
        write_json(ledger_path, _ledger(_round(1, [_finding("finding-a")])))

        result = _run_metrics(ledger_path, _metrics_record())

        assert "append-metrics-requires-current-schema" in result.stderr
        assert not metrics_log.exists()

    def test_refuses_attested_gaps_after_the_baseline(self, ledger_path: Path, metrics_log: Path) -> None:
        """Hold the documented rule that attestation precedes only the baseline, so later records carry null."""
        _seed_rounds(ledger_path, _round(1, [_finding("finding-a")]), _round(2, [_finding("finding-a")]))

        result = _run_metrics(ledger_path, _metrics_record(round=2, attested_gaps=7))

        assert "metrics-attested_gaps-only-on-round-1" in result.stderr
        assert not metrics_log.exists()

    def test_closes_an_unterminated_final_line_and_stores_a_stripped_route(
        self, ledger_path: Path, metrics_log: Path
    ) -> None:
        """Keep every record on its own line and the route joinable even after a partial earlier write."""
        _seed_rounds(ledger_path, _round(1, [_finding("finding-a")]))
        metrics_log.write_bytes(json.dumps(_metrics_record(duration_s=1)).encode("utf-8"))

        result = _run_metrics(ledger_path, _metrics_record(route="bridge:review\n"))

        lines = metrics_log.read_bytes().splitlines()
        assert result.returncode == 0, result.stderr
        assert [json.loads(line)["duration_s"] for line in lines] == [1, 310]
        assert json.loads(lines[1])["route"] == "bridge:review"

    @pytest.mark.parametrize(
        ("route", "warns"),
        [
            pytest.param("agent:foundry:challenger", False, id="agent-route"),
            pytest.param("bridge:review", False, id="bridge-route"),
            pytest.param("parent", False, id="parent-route"),
            pytest.param("other:manual-read", False, id="other-label"),
            pytest.param("foundry:challenger", True, id="missing-kind-prefix"),
            pytest.param("challenger", True, id="bare-role"),
        ],
    )
    def test_warns_without_refusing_an_off_vocabulary_route(
        self, ledger_path: Path, metrics_log: Path, route: str, warns: bool
    ) -> None:
        """Accept any non-blank route but nudge toward the shared vocabulary so route-versus-outcome analysis can
        join."""
        _seed_rounds(ledger_path, _round(1, [_finding("finding-a")]))

        result = _run_metrics(ledger_path, _metrics_record(route=route))

        assert result.returncode == 0, result.stderr
        assert ("metrics-route-not-in-vocabulary" in result.stderr) is warns
        assert len(metrics_log.read_bytes().splitlines()) == 1


class TestPlateauKind:
    """``plateau_kind`` and the ``--plateau-kind`` flag."""

    @pytest.mark.parametrize(
        ("ledger", "expected"),
        [
            pytest.param(_plateau_ledger([_finding("finding-a", "medium")]), "stuck", id="same-signature-persists"),
            pytest.param(
                _plateau_ledger(
                    [_finding("finding-a", "medium", disposition="verified-fixed"), _finding("finding-b", "medium")]
                ),
                "whack-a-mole",
                id="fixed-target-replaced-by-new-finding",
            ),
            pytest.param(
                _plateau_ledger(
                    [
                        _finding("finding-a", "medium"),
                        _finding("finding-b", "low", disposition="verified-fixed"),
                        _finding("finding-c", "low"),
                    ],
                    [_finding("finding-a", "medium"), _finding("finding-b", "low")],
                ),
                "mixed",
                id="carried-and-new-together",
            ),
            pytest.param(
                _plateau_ledger(
                    [
                        _finding(
                            "finding-a", "medium", disposition="verified-fixed", evidence=["invariant: same boundary"]
                        ),
                        _finding("finding-b", "medium", evidence=["invariant: same boundary"]),
                    ],
                    [_finding("finding-a", "medium", evidence=["invariant: same boundary"])],
                ),
                "stuck",
                id="new-route-to-known-invariant-is-not-new",
            ),
            pytest.param(
                _plateau_ledger(
                    [_finding("finding-a", "medium", disposition="verified-fixed"), _finding("finding-b", "low")]
                ),
                None,
                id="score-decreased",
            ),
            pytest.param(
                _plateau_ledger([_finding("finding-a", "medium", disposition="verified-fixed")]), None, id="clean"
            ),
            pytest.param(_ledger(_round(1, [_finding("finding-a")])), None, id="baseline-only"),
        ],
    )
    def test_names_why_the_latest_round_did_not_improve(
        self, loop: ModuleType, ledger: dict[str, Any], expected: str | None
    ) -> None:
        """Tell a fix that never landed from a fix that exposed siblings, without changing the ledger decision.

        Only a round that fails to lower the score has a plateau kind; a fall, a clean result and a lone baseline have
        none.
        """
        assert loop.validate_ledger(ledger | {"schema_version": 2}) == []
        assert loop.plateau_kind(ledger) == expected

    def test_flag_prints_on_stderr_and_keeps_stdout_and_exit_unchanged(
        self, loop: ModuleType, ledger_path: Path
    ) -> None:
        """Add the note only on request, so existing consumers of the JSON summary see no difference."""
        _seed_rounds(
            ledger_path, _round(1, [_finding("finding-a", "medium")]), _round(2, [_finding("finding-a", "medium")])
        )

        plain = _cli(ledger_path)
        flagged = _cli(ledger_path, "--plateau-kind")

        assert flagged.stdout == plain.stdout
        assert flagged.returncode == plain.returncode
        assert plain.stderr == ""
        assert flagged.stderr.strip() == "plateau-kind: stuck"
        assert json.loads(flagged.stdout)["reason"] == "plateau"
        assert loop.load_ledger(ledger_path)["rounds"][1]["index"] == 2

    @pytest.mark.parametrize(
        ("second_round", "expected"),
        [
            pytest.param(
                _round(
                    2, [_finding("finding-a", "medium", disposition="verified-fixed"), _finding("finding-b", "medium")]
                ),
                "whack-a-mole",
                id="siblings-exposed",
            ),
            pytest.param(
                _round(
                    2,
                    [_finding("finding-a", "medium", disposition="verified-fixed"), _finding("finding-b", "medium")],
                    independent=False,
                ),
                "none",
                id="stop-is-not-a-score-plateau",
            ),
        ],
    )
    def test_flag_names_a_non_stuck_kind_only_for_a_score_stop(
        self, ledger_path: Path, second_round: dict[str, Any], expected: str
    ) -> None:
        """Drive the flag through the CLI for a non-stuck kind, and stay silent when the stop is not a score plateau."""
        _seed_rounds(ledger_path, _round(1, [_finding("finding-a", "medium")]), second_round)

        result = _cli(ledger_path, "--plateau-kind")

        assert result.stderr.strip() == f"plateau-kind: {expected}"


class TestBriefArm:
    """``brief_arm`` and the ``--brief-arm`` pilot flag."""

    @pytest.mark.parametrize(
        "other_flag", ["--append", "--append-metrics", "--progress", "--plateau-kind", "--require-clean"]
    )
    def test_refuses_to_swallow_another_action(self, ledger_path: Path, rounds_rec: Path, other_flag: str) -> None:
        """Fail loudly instead of silently skipping a staged append when both flags are given."""
        rounds_rec.write_text(json.dumps(_round(1, [_finding("finding-a")])), encoding="utf-8")

        result = _cli(ledger_path, "--brief-arm", other_flag)

        assert result.returncode == 2
        assert result.stdout == ""
        assert rounds_rec.is_file()

    def test_pause_file_beside_the_run_forces_the_full_wording(self, loop: ModuleType, tmp_path: Path) -> None:
        """Carry the documented lean-arm pause in a file every later run sees, whatever its name hashes to.

        Twenty run directories cover both wordings before the pause file exists; after it is written every one of them
        must report the full wording.
        """
        runs = [tmp_path / f"run-{index}" / "loop-ledger.json" for index in range(20)]
        before = {loop.brief_arm(path) for path in runs}

        (tmp_path / "brief-arm-pause").write_text("lean paused: high finding after lean baseline\n", encoding="utf-8")

        assert before == {"full", "lean"}
        assert {loop.brief_arm(path) for path in runs} == {"full"}

    @pytest.mark.parametrize(
        "run_name",
        [
            pytest.param("run-" + chr(0xDCFF), id="lone-low-surrogate"),
            pytest.param("run-" + chr(0xD800), id="lone-high-surrogate"),
        ],
    )
    def test_survives_a_run_name_that_is_not_valid_utf8(self, loop: ModuleType, run_name: str) -> None:
        """Keep the pilot flag from raising on a POSIX directory whose name holds undecodable bytes."""
        assert loop.brief_arm(Path(run_name) / "loop-ledger.json") in {"full", "lean"}

    @mock.patch.object(Path, "exists", autospec=True, side_effect=PermissionError(13, "Permission denied"))
    def test_survives_an_unsearchable_parent(self, mock_exists: mock.MagicMock, loop: ModuleType) -> None:
        """Return a wording even when the OS refuses the pause-file probe.

        Older Python raises ``PermissionError`` from ``Path.exists`` for an unsearchable directory; the pilot flag must
        never turn that into a crash.
        """
        assert loop.brief_arm(Path("run-a") / "loop-ledger.json") in {"full", "lean"}

    @mock.patch.object(Path, "resolve", autospec=True, side_effect=RuntimeError("Symlink loop"))
    def test_survives_a_symlink_loop(self, mock_resolve: mock.MagicMock, loop: ModuleType) -> None:
        """Return a wording even when path resolution hits a symlink loop.

        Older Python raises ``RuntimeError`` from ``Path.resolve`` for a symlink loop; the pilot flag must never turn
        that into a crash.
        """
        assert loop.brief_arm(Path("run-a") / "loop-ledger.json") in {"full", "lean"}

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot remove the working directory of a process")
    def test_cli_survives_a_deleted_working_directory(self, tmp_path: Path) -> None:
        """Run the real flag from a working directory that no longer exists, where every relative-path OS call fails.

        The earlier fix contained only the exceptions its tests injected, so a fallback that called the working
        directory itself still raised; this drives the shipped command instead of the unit.
        """
        script = (
            "import os, runpy, sys\n"
            "os.mkdir('gone'); os.chdir('gone'); os.rmdir('../gone')\n"
            "sys.argv = ['adversarial_loop.py', '--ledger', 'run-a/loop-ledger.json', '--brief-arm']\n"
            "runpy.run_path(" + repr(str(LEDGER_PATH)) + ", run_name='__main__')\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stderr[-300:]
        assert result.stdout.strip() in {"full", "lean"}

    def test_cli_prints_one_stable_word_and_needs_no_ledger(self, loop: ModuleType, tmp_path: Path) -> None:
        """Assign a pilot brief wording from the run directory name alone, the same every time.

        The flag runs before any round exists, so it must not read the ledger, and it must never alter files or stdout
        shape beyond one word.
        """
        ledger_path = tmp_path / "run-0" / "loop-ledger.json"

        first = _cli(ledger_path, "--brief-arm")
        again = _cli(ledger_path, "--brief-arm")

        assert first.returncode == 0, first.stderr
        assert first.stdout == again.stdout
        assert first.stdout.strip() == loop.brief_arm(ledger_path)
        assert not (tmp_path / "run-0").exists()

    def test_runs_split_across_both_wordings(self, loop: ModuleType, tmp_path: Path) -> None:
        """Split a pilot of 40 runs between the full and lean wordings in roughly equal shares."""
        arms = [loop.brief_arm(tmp_path / f"run-{index}" / "loop-ledger.json") for index in range(40)]

        assert set(arms) == {"full", "lean"}
        assert 8 <= arms.count("lean") <= 32


class TestLedgerStorage:
    """On-disk layout: file naming, historical inline ledgers and the ledger digest."""

    @pytest.mark.parametrize(
        ("ledger_name", "metrics_name"),
        [
            pytest.param("loop-ledger.json", "loop-metrics.jsonl", id="current-prefix"),
            pytest.param("prior-loop-ledger.json", "prior-loop-metrics.jsonl", id="copied-prior-prefix"),
        ],
    )
    def test_metrics_path_follows_the_ledger_prefix(
        self, loop: ModuleType, tmp_path: Path, ledger_name: str, metrics_name: str
    ) -> None:
        """Keep a copied prior ledger's cost log beside it, as the round log already does."""
        assert loop.metrics_path(tmp_path / ledger_name).name == metrics_name

    def test_historical_inline_ledger_stays_readable_but_cannot_grow(self, loop: ModuleType, tmp_path: Path) -> None:
        """Read a schema-1 archive unchanged, while refusing to append rounds to the rewritten single-file shape."""
        ledger_path = tmp_path / "loop-ledger.json"
        historical = _ledger(_round(1, [_finding("finding-a")]))
        write_json(ledger_path, historical)

        appended = _append(ledger_path, _round(2, [_finding("finding-a")]))

        assert loop.load_ledger(ledger_path) == historical
        assert loop.validate_ledger(loop.load_ledger(ledger_path)) == []
        assert appended.returncode == 1
        assert "append-requires-current-schema" in json.loads(appended.stderr)["errors"]
        assert json.loads(ledger_path.read_text(encoding="utf-8")) == historical

    def test_ledger_digest_binds_historical_bytes_and_current_round_log(self, loop: ModuleType, tmp_path: Path) -> None:
        """Keep archived continuation digests valid and make any round-log change visible in the current digest."""
        historical = tmp_path / "historical" / "loop-ledger.json"
        historical.parent.mkdir()
        historical.write_bytes(json.dumps(_ledger(_round(1, [_finding("finding-a")]))).encode())
        current = tmp_path / "current" / "loop-ledger.json"
        current.parent.mkdir()
        _write_header(current)
        empty_digest = loop.ledger_digest(current)
        _seed_rounds(current, _round(1, [_finding("finding-a")]))
        one_round_digest = loop.ledger_digest(current)
        rounds_log = current.with_name("loop-rounds.jsonl")
        rounds_log.write_bytes(rounds_log.read_bytes() + b" ")

        assert loop.ledger_digest(historical) == hashlib.sha256(historical.read_bytes()).hexdigest()
        assert len({empty_digest, one_round_digest, loop.ledger_digest(current)}) == 3


@pytest.mark.integration
class TestImprovingRounds:
    """A five-round correction driven through the real append and gate commands."""

    def test_five_improving_rounds_append_to_clean_without_rewriting_history(
        self, loop: ModuleType, ledger_path: Path
    ) -> None:
        """Permit a monotone five-round correction while preserving append-only identity.

        Each append must keep every earlier log byte intact, report the growing score list, and end clean on round 5.
        """
        steps = _append_each(ledger_path, _improving_rounds())

        results = [result for result, _ in steps]
        logs = [b"", *(log for _, log in steps)]
        summaries = [json.loads(result.stdout) for result in results]
        assert [result.returncode for result in results] == [0] * 5, [result.stderr for result in results]
        assert [summary["scores"] for summary in summaries] == [
            [40],
            [40, 20],
            [40, 20, 10],
            [40, 20, 10, 4],
            [40, 20, 10, 4, 0],
        ]
        assert [summary["reason"] for summary in summaries] == [
            "baseline",
            "converging",
            "converging",
            "converging",
            "clean",
        ]
        assert all(new.startswith(old) for old, new in pairwise(logs))
        assert [len(log.splitlines()) for log in logs[1:]] == [1, 2, 3, 4, 5]
        assert loop.load_ledger(ledger_path)["schema_version"] == loop.SCHEMA_VERSION == 2

    def test_clean_ledger_passes_the_require_clean_gate(self, ledger_path: Path) -> None:
        """A ledger whose five rounds end with nothing open passes ``--require-clean``."""
        _seed_rounds(ledger_path, *_improving_rounds())

        clean = _cli(ledger_path, "--require-clean")

        assert clean.returncode == 0, clean.stderr
        assert json.loads(clean.stdout)["reason"] == "clean"

    def test_round_after_clean_is_refused_and_log_unchanged(self, ledger_path: Path, rounds_log: Path) -> None:
        """Refuse a sixth round after the ledger ended clean, leaving the round log untouched."""
        rounds = _improving_rounds()
        _seed_rounds(ledger_path, *rounds)
        before = rounds_log.read_bytes()

        after_clean = _append(ledger_path, _round(6, rounds[-1]["findings"]))

        assert after_clean.returncode == 1
        assert _REMAINING_FINDING_ERROR in json.loads(after_clean.stderr)["errors"]
        assert rounds_log.read_bytes() == before


class TestActionCoverage:
    """``validate_actions``: every open finding needs a bound, evidenced decision."""

    @pytest.fixture
    def boundary_ledger(self) -> dict[str, Any]:
        """Build a ledger with one high and one low open finding."""
        return _ledger(_round(1, [_finding("boundary"), _finding("typo", "low")]))

    @pytest.fixture
    def boundary_actions(self) -> dict[str, Any]:
        """Build a complete actions document: escalate the high finding, defer the low one."""
        return _actions(
            [
                _action(
                    "boundary",
                    "escalate",
                    ["Contract change exceeds current authority."],
                    "Request approval for the contract change.",
                ),
                _action(
                    "typo",
                    "defer",
                    ["Copy change is outside the approved file set."],
                    "Request the copy file in scope.",
                ),
            ],
            version=1,
        )

    def test_escalating_and_deferring_every_open_finding_is_valid(
        self, loop: ModuleType, boundary_ledger: dict[str, Any], boundary_actions: dict[str, Any]
    ) -> None:
        """Accept an action for every open finding when the severe one is escalated rather than deferred."""
        assert loop.validate_actions(boundary_ledger, boundary_actions) == []

    def test_missing_action_for_an_open_finding_is_reported(
        self, loop: ModuleType, boundary_ledger: dict[str, Any], boundary_actions: dict[str, Any]
    ) -> None:
        """Reject silent omission of an unresolved finding.

        Dropping the action for the low finding leaves it unaccounted for.
        """
        boundary_actions["rounds"][0]["actions"].pop()

        assert "round-1-action-missing:typo" in loop.validate_actions(boundary_ledger, boundary_actions)

    def test_deferring_a_severe_finding_is_refused(
        self, loop: ModuleType, boundary_ledger: dict[str, Any], boundary_actions: dict[str, Any]
    ) -> None:
        """Reject deferral of an unresolved high-severity finding; it must be fixed or escalated."""
        boundary_actions["rounds"][0]["actions"][0]["decision"] = "defer"

        assert "round-1-severe-finding-must-fix-or-escalate:boundary" in loop.validate_actions(
            boundary_ledger, boundary_actions
        )

    @pytest.mark.parametrize(
        ("root_cause", "expected"),
        [
            pytest.param(None, ["round-2-repeat-root-cause-required:repeat"], id="without-root-cause"),
            pytest.param(_ROOT_CAUSE, [], id="with-root-cause"),
        ],
    )
    def test_repeated_open_finding_requires_root_cause_before_another_fix(
        self, loop: ModuleType, root_cause: dict[str, str] | None, expected: list[str]
    ) -> None:
        """Tie a repeat-fix attempt to a falsifiable cause, not merely an improving score.

        ``repeat`` is still open in round 2 and fixed again, so the second fix must carry a root cause; the new low
        finding is deferred.
        """
        ledger = _ledger(_round(1, [_finding("repeat")]), _round(2, [_finding("repeat"), _finding("new", "low")]))
        fix = _action(
            "repeat",
            "fix",
            ["Changed parser boundary and added regression."],
            "Request independent verification on the corrected snapshot.",
        )
        deferred = _action("new", "defer", ["Low-severity copy change needs a separate scope."], fix["next_action"])
        actions = _actions([fix], [{**fix, "root_cause": root_cause}, deferred], version=1)

        assert loop.validate_actions(ledger, actions) == expected

    @pytest.mark.parametrize(
        ("extra_evidence", "expected"),
        [
            pytest.param([], ["round-1-fix-evidence-missing:sibling:external-write"], id="sibling-missing"),
            pytest.param(["sibling: existing backup symlink failed before and passed after"], [], id="sibling-present"),
        ],
    )
    def test_current_fix_action_requires_invariant_and_sibling_evidence(
        self, loop: ModuleType, extra_evidence: list[str], expected: list[str]
    ) -> None:
        """Block a fix record that proves only the original example.

        The fix names the invariant, the original case, the consumer and the source paths, but needs a sibling check
        too.
        """
        ledger = _ledger(_round(1, [_finding("external-write")]))
        evidence = [
            "invariant: release preparation cannot write outside its directory",
            "original: release-directory symlink regression failed before and passed after",
            "consumer: setup release command returned an error before any external write",
            "source-paths: setup_release_dir.py and its regression test",
            *extra_evidence,
        ]
        action = _action("external-write", "fix", evidence, "Seek a fresh independent challenge.")

        assert loop.validate_actions(ledger, _actions([action], version=2)) == expected

    @pytest.mark.parametrize(
        "version", [0, 4, True, "3", 3.0, pytest.param([], id="list"), pytest.param({}, id="dict")]
    )
    def test_actions_schema_outside_known_versions_is_rejected(self, loop: ModuleType, version: object) -> None:
        """Only actions schemas 1, 2 and 3 are accepted; a boolean is not an integer version."""
        ledger = _ledger(_round(1, [_finding("typo", "low")])) | {"schema_version": 2}
        actions = _actions([_held_action("typo", "copy matches the spec", "defer")], version=version)

        assert "actions-schema-version-invalid" in loop.validate_actions(ledger, actions)

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
            pytest.param(
                "escalate", ["invariant:   ", "needs owner"], "action-evidence-missing:invariant:", id="blank"
            ),
        ],
    )
    def test_current_actions_require_invariant_on_every_decision(
        self, loop: ModuleType, decision: str, evidence: list[str], expected: str
    ) -> None:
        """Schema 3 rejects any action, whatever its decision, that does not name a nonempty invariant.

        Without the entry an escalated or deferred finding has no invariant key, and a later route to it slips past the
        repeat and third-occurrence rules.
        """
        ledger = _ledger(_round(1, [_finding("typo", "low")])) | {"schema_version": 2}
        action = _action("typo", decision, evidence, "Request the owner decision.")

        assert loop.validate_actions(ledger, _actions([action], version=3)) == [f"round-1-{expected}typo"]

    def test_historical_schema_two_actions_keep_their_evidence_rules(self, loop: ModuleType) -> None:
        """A schema-2 action record without ``invariant:`` on escalated or deferred findings still validates as before.

        Archived schema-2 records were written when only a fix named its invariant; they stay readable under that rule
        while current results require schema 3.
        """
        ledger = _ledger(_round(1, [_finding("boundary"), _finding("typo", "low")])) | {"schema_version": 2}
        actions = _actions(
            [
                _action("boundary", "escalate", ["Needs an owner decision."], "Ask."),
                _action("typo", "defer", ["Needs an owner decision."], "Ask."),
            ]
        )

        assert loop.validate_actions(ledger, actions) == []

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
        self, loop: ModuleType, decision: str, finding_evidence: list[str], expected: list[str]
    ) -> None:
        """Every action invariant must match normalized finding evidence, for all schema-3 decisions."""
        ledger = _ledger(_round(1, [_finding("boundary", "low", evidence=finding_evidence)]))
        action = _invariant_action("boundary")
        action["decision"] = decision
        action["evidence"][:1] = ["invariant:  STRASSE boundary ", "invariant: second  BOUNDARY"]

        assert loop.validate_actions(ledger, _actions([action], version=3)) == expected

    @pytest.mark.parametrize("label", ["Invariant:", "INVARIANT:"])
    @pytest.mark.parametrize(
        ("action_invariant", "expected"),
        [
            pytest.param("every route is reviewed", [], id="matching-invariant"),
            pytest.param(
                "different boundary", ["round-1-action-invariant-not-in-finding:boundary"], id="different-invariant"
            ),
        ],
    )
    def test_action_matches_reviewer_invariant_label_without_changing_evidence(
        self, loop: ModuleType, label: str, action_invariant: str, expected: list[str]
    ) -> None:
        """Accept label casing while still requiring the action invariant to match the review evidence.

        The reviewer wrote the label in a different case; the comparison ignores the case but never rewrites the stored
        finding evidence.
        """
        finding = _finding("boundary", "low", evidence=[f"{label} Every route is reviewed"])
        ledger = _ledger(_round(1, [finding]))
        action = _invariant_action("boundary")
        action["evidence"][0] = f"invariant: {action_invariant}"

        assert loop.validate_actions(ledger, _actions([action], version=3)) == expected
        assert finding["evidence"] == [f"{label} Every route is reviewed"]

    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            pytest.param(1, [], id="schema-one-accepts"),
            pytest.param(2, [], id="schema-two-accepts"),
            pytest.param(
                3,
                ["round-1-action-invariant-not-in-finding:a", "round-2-action-invariant-not-in-finding:b"],
                id="schema-three-rejects",
            ),
        ],
    )
    def test_action_only_invariant_cannot_connect_distinct_ledger_findings(
        self, loop: ModuleType, version: int, expected: list[str]
    ) -> None:
        """Only schema 3 rejects an action-only connection that progress cannot see in ledger evidence."""
        ledger = _route_ledger(["a"], ["b"], invariants={"a": "first boundary", "b": "second boundary"})
        actions = _actions(
            [_reworded_action("a", "shared boundary")],
            [_reworded_action("b", "shared boundary", _ROOT_CAUSE)],
            version=version,
        )

        assert loop.validate_ledger(ledger) == []
        assert "| 2 | 0 + 1 |" in loop._render_progress(ledger, 2)
        assert loop.validate_actions(ledger, actions) == expected


class TestThirdOccurrence:
    """Reopened and repeated findings: the third occurrence must stop."""

    def test_reopened_third_occurrence_cannot_be_fixed_despite_improving_total(self, loop: ModuleType) -> None:
        """Count reopened signatures cumulatively so closing other findings cannot hide a third recurrence."""
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
                        _action(
                            signature,
                            "fix" if signature == "repeat" else "escalate",
                            ["Retained independent recurrence evidence"],
                            "Seek independent verification",
                            # a reopened invariant needs a cause before each later fix; this isolates the third stop
                            _ROOT_CAUSE if signature == "repeat" else None,
                        )
                        for signature in tiers
                        if signature not in closed
                    ],
                }
                for index, closed in enumerate(closed_by_round, start=1)
            ],
        }

        assert loop.validate_ledger(ledger) == []
        assert loop.summarize_ledger(ledger)["scores"] == [43, 21, 13, 5, 3]
        assert loop.validate_actions(ledger, actions) == ["round-5-third-occurrence-must-stop:repeat"]

    def test_third_route_to_known_invariant_must_stop(self, loop: ModuleType) -> None:
        """Three route-named signatures of one invariant count as its third occurrence."""
        # escalated padding keeps the score strictly decreasing, so round 3 is not refused after a plateau stop
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
            [
                _invariant_action("route-one"),
                _held_action("pad-1", "padding one"),
                _held_action("pad-2", "padding two"),
            ],
            [_invariant_action("route-two", _ROOT_CAUSE), _held_action("pad-1", "padding one")],
            [_invariant_action("route-three", _ROOT_CAUSE)],
            version=3,
        )

        assert loop.validate_actions(ledger, actions) == ["round-3-third-occurrence-must-stop:route-three"]

    @pytest.mark.parametrize(
        ("route_two_root_cause", "expected"),
        [
            pytest.param(
                None,
                ["round-3-repeat-root-cause-required:route-two", "round-3-third-occurrence-must-stop:route-two"],
                id="without-root-cause",
            ),
            pytest.param(_ROOT_CAUSE, ["round-3-third-occurrence-must-stop:route-two"], id="with-root-cause"),
        ],
    )
    def test_invariant_reopened_after_closed_round_is_third_occurrence(
        self, loop: ModuleType, route_two_root_cause: dict[str, str] | None, expected: list[str]
    ) -> None:
        """A verified-fixed carry still enters history, so reopening after two recorded rounds must stop."""
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
            [_invariant_action("route-two", route_two_root_cause)],
            version=3,
        )

        assert loop.validate_actions(ledger, actions) == expected

    def test_shared_invariant_round_counts_once_toward_third_occurrence(self, loop: ModuleType) -> None:
        """A round where two routes share an invariant is one occurrence, so the third fix is two rounds later, not one.

        Round 1 records the invariant twice, round 2 fixes a new route with root cause as its second occurrence, and
        round 3 is the first round with two earlier rounds behind it.
        """
        # escalated padding keeps the score strictly decreasing, so round 3 is not refused after a plateau stop
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

        assert loop.validate_actions(ledger, actions) == ["round-3-third-occurrence-must-stop:route-four"]

    def test_keys_from_two_earlier_rounds_make_a_third_occurrence(self, loop: ModuleType) -> None:
        """A fix whose signature matches one earlier round and whose invariant matches another is a third occurrence.

        Counting each key on its own would see one earlier round per key; the earlier occurrences are the union of the
        rounds any identity key appeared in.
        """
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

        assert loop.validate_actions(ledger, actions) == ["round-3-third-occurrence-must-stop:sig-a"]

    def test_consistent_ledger_invariant_history_with_third_occurrence_escalated_is_valid(
        self, loop: ModuleType
    ) -> None:
        """A repeated fix with root cause and escalation at the third occurrence remains valid."""
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

        assert loop.validate_actions(ledger, actions) == []


class TestInvariantRecurrence:
    """Schema-3 recurrence: signatures and invariants jointly identify a repeated finding."""

    def test_invariant_label_case_does_not_hide_recurring_finding(self, loop: ModuleType) -> None:
        """Require root-cause evidence when a renamed finding repeats a mixed-case reviewer invariant."""
        ledger = _route_ledger(
            ["first"], ["second"], invariants=dict.fromkeys(("first", "second"), "every route is reviewed")
        )
        ledger["rounds"][0]["findings"][0]["evidence"] = ["Invariant: every route is reviewed"]
        first = _invariant_action("first")
        second = _invariant_action("second")
        first["evidence"][0] = second["evidence"][0] = "invariant: every route is reviewed"

        assert loop.validate_actions(ledger, _actions([first], [second], version=3)) == [
            "round-2-repeat-root-cause-required:second"
        ]

    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            pytest.param(1, [], id="schema-one-signature-only"),
            pytest.param(2, [], id="schema-two-signature-only"),
            pytest.param(3, ["round-2-repeat-root-cause-required:route-two"], id="schema-three-invariant-repeat"),
        ],
    )
    def test_new_route_to_known_invariant_requires_root_cause(
        self, loop: ModuleType, version: int, expected: list[str]
    ) -> None:
        """Only schema 3 treats a sibling signature with the same invariant as a repeat.

        A filter loop named each round's bypass after its route, so the signature never repeated and the repeat root-
        cause rule never fired; schema 3 uses the shared ``invariant:`` evidence to identify that recurrence.
        """
        ledger = _route_ledger(
            ["route-one"],
            ["route-two"],
            invariants=dict.fromkeys(("route-one", "route-two"), "Every case arm is judged as a command"),
        )
        actions = _actions([_invariant_action("route-one")], [_invariant_action("route-two")], version=version)

        assert loop.validate_actions(ledger, actions) == expected

    def test_reworded_invariant_under_known_signature_is_repeat(self, loop: ModuleType) -> None:
        """A known signature stays a repeat when the parent rewords its invariant in a later round.

        Keying recurrence on the invariant text alone let one rewording hide the second fix of the same finding, a
        regression from signature-keyed counting; the signature is still part of the identity.
        """
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

        assert loop.validate_actions(ledger, actions) == ["round-2-repeat-root-cause-required:case-arm-body-judged"]

    @pytest.mark.parametrize("decision", ["escalate", "defer"])
    def test_new_route_after_unfixed_invariant_is_repeat(self, loop: ModuleType, decision: str) -> None:
        """An invariant first escalated or deferred still counts as an earlier occurrence when a new route is fixed.

        Only fixes had to name their invariant, so a round that escalated the finding left no key and the next route
        read as a first occurrence. Schema 3 makes every decision carry the ``invariant:`` entry the recurrence count
        needs.
        """
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

        assert loop.validate_actions(ledger, actions) == ["round-2-repeat-root-cause-required:route-two"]

    def test_shared_invariant_within_one_round_is_one_occurrence(self, loop: ModuleType) -> None:
        """Two signatures of one invariant in the same round are accepted, neither a repeat nor an error.

        A reviewer can report two routes to one invariant at once; the parent fixes both under one closure plan, and the
        round is that invariant's first occurrence.
        """
        ledger = _route_ledger(
            ["route-one", "route-two"],
            invariants=dict.fromkeys(("route-one", "route-two"), "Every case arm is judged as a command"),
        )
        actions = _actions([_invariant_action("route-one"), _invariant_action("route-two")], version=3)

        assert loop.validate_actions(ledger, actions) == []

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
        self, loop: ModuleType, signature: str, invariant: str, root_cause: dict[str, str] | None, expected: list[str]
    ) -> None:
        """Rewording a known signature joins both invariant keys before the next route is counted."""
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

        assert loop.validate_actions(ledger, actions) == expected

    @pytest.mark.parametrize(
        ("root_cause", "expected"),
        [
            pytest.param(None, ["round-2-repeat-root-cause-required:route-c"], id="without-root-cause"),
            pytest.param(_ROOT_CAUSE, [], id="with-root-cause"),
        ],
    )
    def test_two_round_key_chain_is_only_a_repeat(
        self, loop: ModuleType, root_cause: dict[str, str] | None, expected: list[str]
    ) -> None:
        """A chain formed by two same-round actions has only one earlier round behind the next fix."""
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
            [_reworded_action("route-c", "first wording", root_cause)],
            version=3,
        )

        assert loop.validate_actions(ledger, actions) == expected

    def test_current_round_connections_do_not_change_sibling_recurrence(self, loop: ModuleType) -> None:
        """A connection created by one action is published only after its siblings have been validated."""
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
            [
                _reworded_action("route-a", "second wording", _ROOT_CAUSE),
                _reworded_action("route-b", "second wording"),
            ],
            version=3,
        )

        assert loop.validate_actions(ledger, actions) == []

    @pytest.mark.parametrize("version", [1, 2, 3])
    def test_ledger_invariants_count_when_actions_name_unrelated_invariants(
        self, loop: ModuleType, version: int
    ) -> None:
        """Schema 3 rejects unrelated action wording and still counts the ledger's invariant history."""
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
        expected = {
            3: [
                "round-1-action-invariant-not-in-finding:a",
                "round-2-action-invariant-not-in-finding:b",
                "round-2-repeat-root-cause-required:b",
                "round-3-action-invariant-not-in-finding:c",
                "round-3-repeat-root-cause-required:c",
                "round-3-third-occurrence-must-stop:c",
            ]
        }.get(version, [])

        assert loop.validate_ledger(ledger) == []
        progress = loop._render_progress(ledger, 3)
        assert "| 2 | 2 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 40 + 0 |" in progress
        assert "| 3 | 1 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 20 + 0 |" in progress
        assert loop.validate_actions(ledger, actions) == expected


class TestHistoricalActionSchemas:
    """Archived action schemas 1 and 2 keep signature-only recurrence."""

    @pytest.mark.parametrize("version", [1, 2])
    @pytest.mark.parametrize(
        ("signatures", "action_rounds", "expected"),
        [
            pytest.param(
                [["repeat"], ["repeat"]],
                [[_invariant_action("repeat")], [_invariant_action("repeat")]],
                ["round-2-repeat-root-cause-required:repeat"],
                id="signature-open-in-consecutive-rounds",
            ),
            pytest.param(
                [["repeat", "padding"], ["padding"], ["repeat"]],
                [
                    [_invariant_action("repeat"), _held_action("padding", "unrelated")],
                    [_held_action("padding", "unrelated")],
                    [_invariant_action("repeat")],
                ],
                [],
                id="reopened-after-a-gap-is-not-a-repeat",
            ),
            pytest.param(
                [["repeat", "pad-1", "pad-2"], ["repeat", "pad-1"], ["repeat"]],
                [
                    [_held_action("pad-1", "padding one"), _held_action("pad-2", "padding two")],
                    [_held_action("pad-1", "padding one")],
                    [_invariant_action("repeat")],
                ],
                [
                    "round-1-action-missing:repeat",
                    "round-2-action-missing:repeat",
                    "round-3-repeat-root-cause-required:repeat",
                    "round-3-third-occurrence-must-stop:repeat",
                ],
                id="findings-without-actions-still-count",
            ),
            pytest.param(
                [["route-a", "pad-1", "pad-2"], ["route-a", "pad-1"], ["route-b"]],
                [
                    [
                        _reworded_action("route-a", "first wording"),
                        _held_action("pad-1", "pad one holds"),
                        _held_action("pad-2", "pad two holds"),
                    ],
                    [
                        _reworded_action("route-a", "second wording", _ROOT_CAUSE),
                        _held_action("pad-1", "pad one holds"),
                    ],
                    [_reworded_action("route-b", "second wording", _ROOT_CAUSE)],
                ],
                [],
                id="shared-invariant-evidence-is-ignored",
            ),
        ],
    )
    def test_signature_only_recurrence(
        self,
        loop: ModuleType,
        version: int,
        signatures: list[list[str]],
        action_rounds: list[list[dict[str, Any]]],
        expected: list[str],
    ) -> None:
        """Archived action schemas count a repeat by signature alone and never connect signatures by invariant.

        Cases: a signature open in consecutive rounds needs a root cause; a signature separated by a closed round is a
        first occurrence; open findings count toward repeats even without actions; a shared invariant joins nothing.
        """
        ledger = _route_ledger(*signatures)

        assert loop.validate_actions(ledger, _actions(*action_rounds, version=version)) == expected


class TestDispositionHistory:
    """Progress scores and recurrence agree on how every disposition enters history."""

    @staticmethod
    def _ledger_for(disposition: str) -> dict[str, Any]:
        """Build a two-round ledger whose first report of ``a`` carries ``disposition`` and whose route ``b`` repeats
        it."""
        invariant = "The same boundary holds"
        first = _finding("a", "high", disposition, evidence=[f"invariant: {invariant}", "review evidence"])
        closed = {**first, "disposition": "verified-fixed"}
        return _ledger(
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

    @pytest.mark.parametrize(
        ("disposition", "first_score"),
        [
            pytest.param("open", 26, id="open"),
            pytest.param("fixed-pending-verification", 26, id="fixed-pending-verification"),
            pytest.param("verified-fixed", 20, id="verified-fixed"),
            pytest.param("rejected", 20, id="rejected"),
        ],
    )
    def test_scores_and_progress_count_every_disposition_as_history(
        self, loop: ModuleType, disposition: str, first_score: int
    ) -> None:
        """Only an unresolved first report adds weight to round 1, and route ``b`` is old weight in round 2."""
        ledger = self._ledger_for(disposition)

        assert loop.validate_ledger(ledger) == []
        assert loop.summarize_ledger(ledger)["scores"] == [first_score, 6]
        assert "| 2 | 0 + 0 | 0 + 0 | 1 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 6 + 0 |" in loop._render_progress(ledger, 2)

    @pytest.mark.parametrize(
        ("disposition", "a_needs_action"),
        [
            pytest.param("open", True, id="open"),
            pytest.param("fixed-pending-verification", True, id="fixed-pending-verification"),
            pytest.param("verified-fixed", False, id="verified-fixed"),
            pytest.param("rejected", False, id="rejected"),
        ],
    )
    @pytest.mark.parametrize(
        ("root_cause", "expected"),
        [
            pytest.param(None, ["round-2-repeat-root-cause-required:b"], id="without-root-cause"),
            pytest.param(_ROOT_CAUSE, [], id="with-root-cause"),
        ],
    )
    def test_closed_or_rejected_first_report_makes_a_later_route_a_repeat(
        self,
        loop: ModuleType,
        disposition: str,
        a_needs_action: bool,
        root_cause: dict[str, str] | None,
        expected: list[str],
    ) -> None:
        """A closed or rejected first report still makes a later route old and requires root cause for its fix."""
        invariant = "The same boundary holds"
        round_one = [_held_action("padding", "unrelated")]
        round_one = [_held_action("a", invariant), *round_one] if a_needs_action else round_one
        actions = _actions(round_one, [_reworded_action("b", invariant, root_cause)], version=3)

        assert loop.validate_actions(self._ledger_for(disposition), actions) == expected


class TestLoopRuleProse:
    """The shipped loop documents carry the rules the helper and skills rely on."""

    def test_specialist_retry_policy_permits_corrected_reviewer_redispatch(self) -> None:
        """The Codex Rig retry policy carries the loop rule's one corrected reviewer re-dispatch.

        The policy otherwise retries only timeout, transport and rate-limit failures, so without this exception a
        reviewer round that returned nothing or a ``blocked`` envelope would never get the re-dispatch the loop rule
        requires.
        """
        policy = _prose("specialist-orchestration.md")
        loop_rule = _prose("adversarial-loop.md")

        assert (
            _missing(
                policy,
                [
                    "gets one corrected re-dispatch with a brief that removes the recorded cause",
                    "counts toward the 2-attempt cap and is never an identical retry",
                    "When the cap is already spent, the route is failed",
                    "Strict portable `parallel_execution` nodes are excluded",
                ],
            )
            == []
        )
        assert (
            _missing(
                loop_rule,
                [
                    "Re-dispatch that reviewer once with a brief that removes the cause;",
                    "When no attempt remains, the required approval is not held, or no brief can remove the cause",
                ],
            )
            == []
        )

    def test_loop_rule_maps_the_agent_usage_notification_to_the_cost_log(self) -> None:
        """The loop rule tells a Claude parent which notification fields fill the cost log's token total and duration.

        Without that mapping the parent has no source for the numbers and writes ``null``, which left every recorded
        token count unknown before schema 2.
        """
        loop_rule = _prose("adversarial-loop.md")

        assert (
            _missing(
                loop_rule,
                [
                    "record `subagent_tokens` as `tokens_total` and `duration_ms` divided by 1000 as `duration_s`",
                    "Schema 1 records (`schema_version` 1, no `tokens_total`) stay accepted",
                ],
            )
            == []
        )

    def test_loop_rule_records_replayable_probe_command_lines(self) -> None:
        """The loop rule asks for the failing-before/passing-after check as a literal command line.

        The attestation and a ``fix`` action both carry it, so the parent can rerun the same probe after a later edit.
        The helper stays a recorder: the rule says it never runs the line, and a reviewer never gets it as a resolver
        account.
        """
        loop_rule = _prose("adversarial-loop.md")

        assert (
            _missing(
                loop_rule,
                [
                    "a failing-before/passing-after check as its literal command line plus its result",
                    "a `probe:` entry holding the literal failing-before/passing-after command line",
                    "the helper never runs it; a reviewer sees it only as its own original counterexample",
                ],
            )
            == []
        )
