"""Regression checks for the bounded reasoning-progress escalation ledger.

The ledger validator decides when repeated failed attempts must escalate to an advisor or a human, and which later
evidence may resume ordinary work. In-process cases pin each validator verdict; CLI cases pin the append-only file
contract through the real standalone script.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
LEDGER_PATH = PLUGIN_ROOT / "shared" / "escalation_ledger.py"
STALL = "escalation-required-after-stall-trigger"
RECOVERED = {"action": "attempt-1", "material_progress": True, "closure_met": False}


@pytest.fixture(scope="module")
def escalation_ledger() -> ModuleType:
    """Load the standalone ledger helper without package installation."""
    specification = importlib.util.spec_from_file_location("codex_rig_escalation_ledger", LEDGER_PATH)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _cycle(index: int, material_progress: bool) -> dict[str, Any]:
    """Build one valid cycle record with evidence when progress is claimed.

    Example:
        >>> _cycle(1, True)["evidence"]
        ['evidence-1']
    """
    return {
        "index": index,
        "objective": "close failing acceptance check",
        "operation": f"attempt-{index}",
        "outcome": "acceptance condition remains open",
        "next_decision": "record the observed result",
        "material_progress": material_progress,
        "work_kind": "primary",
        "evidence": [f"evidence-{index}"] if material_progress else [],
    }


def _auxiliary(cycle: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Relabel a cycle as auxiliary setup work required for independent review evidence.

    Example:
        >>> _auxiliary(_cycle(2, False))["work_kind"]
        'auxiliary'
    """
    return {**cycle, "work_kind": "auxiliary", "required_for": "independent review evidence", **fields}


def _ledger(*cycles: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Build a minimally valid in-progress ledger for one open condition, with optional field overrides.

    Example:
        >>> _ledger()["closure_condition"]["status"]
        'open'
    """
    return {
        "schema_version": 3,
        "primary_goal": "Fix the observed runtime defect.",
        "workstream_id": "fixture-workstream",
        "closure_condition": {"id": "acceptance-check", "status": "open"},
        "cycles": list(cycles),
        "outcome": "working",
        **fields,
    }


def _advisory() -> dict[str, Any]:
    """Build an observed read-only advisory record.

    Example:
        >>> _advisory()["observed_sandbox"]
        'read-only'
    """
    return {
        "requested_model": "gpt-5.6-terra",
        "requested_effort": "high",
        "observed_model": "gpt-5.6-terra",
        "observed_effort": "high",
        "observed_sandbox": "read-only",
        "mode": "advice-only",
        "state_changes": [],
        "recommendation": "run one bounded recovery check",
        "stop_condition": "handoff if acceptance remains open",
    }


def _handoff() -> dict[str, Any]:
    """Build the required evidence-backed human handoff record.

    Example:
        >>> _handoff()["alternatives"]
        ['defer the workstream']
    """
    return {
        "summary": "The bounded recovery did not close the condition.",
        "recommended_next_step": "Choose the proposed recovery direction.",
        "alternatives": ["defer the workstream"],
    }


def _recovering(*cycles: dict[str, Any], recovery: dict[str, Any] = RECOVERED, **fields: Any) -> dict[str, Any]:
    """Build a ledger that records an advisory plus a recovery action after its cycles.

    Example:
        >>> _recovering()["recovery"]["action"]
        'attempt-1'
    """
    return _ledger(*cycles, advisory=_advisory(), recovery=recovery, **fields)


def _replaced(**replacement: Any) -> dict[str, Any]:
    """Build a ledger whose closure condition was replaced, with replacement fields overridden.

    Example:
        >>> _replaced(id="other")["closure_condition"]["replacement"]["id"]
        'other'
    """
    proof = {"id": "revised-acceptance", "source": "user-direction", "evidence": ["Retained user decision."]}
    closure = {"id": "acceptance-check", "status": "replaced", "replacement": {**proof, **replacement}}
    return _ledger(_cycle(1, True), _cycle(2, True), _cycle(3, True), closure_condition=closure)


def _diagnostic_sequence() -> dict[str, Any]:
    """Build six observed cycles where progress at cycles 2 and 5 splits the failures, with one auxiliary row.

    Example:
        >>> len(_diagnostic_sequence()["cycles"])
        6
    """
    cycles = [
        _cycle(index, index in {2, 5}) | {"evidence": [f"Observed diagnostic result {index}."]} for index in range(1, 7)
    ]
    cycles[3] = _auxiliary(cycles[3])
    return _ledger(*cycles)


_STALLED = _ledger(_cycle(1, False), _cycle(2, False))
_ARCHIVE = _ledger(_cycle(1, True), schema_version=2)


class TestValidateLedgerAccepts:
    """Ledgers that are honest about progress, escalation and recovery pass validation."""

    @pytest.mark.parametrize(
        "ledger",
        [
            # the stall trigger is satisfied once the ledger records the read-only advisory
            pytest.param(_ledger(*_STALLED["cycles"], outcome="advisory", advisory=_advisory()), id="stall-escalated"),
            # a user decision is evidence, so a cycle backed only by it is not evidence-free
            pytest.param(
                _ledger(_cycle(1, False), _cycle(2, True) | {"evidence": ["user approved narrowed scope"]}),
                id="user-directed-progress",
            ),
            # evidence-backed diagnostics keep working while their acceptance condition stays open
            pytest.param(_ledger(_cycle(1, True), _cycle(2, True), _cycle(3, True)), id="productive-nonclosing"),
            # one failed diagnostic between two productive cycles is not three failed attempts
            pytest.param(
                _ledger(
                    _cycle(1, True),
                    _cycle(2, False) | {"evidence": ["Acceptance check still fails after the second attempted fix."]},
                    _cycle(3, True),
                ),
                id="mixed-productive",
            ),
            # an auxiliary setup row between them must not count as an attempt either
            pytest.param(
                _ledger(
                    _cycle(1, True),
                    _cycle(2, False) | {"evidence": ["Acceptance check still fails after the second attempted fix."]},
                    _auxiliary(_cycle(3, False), evidence=["receipt repair"]),
                    _cycle(4, True),
                ),
                id="mixed-productive-with-auxiliary",
            ),
            # administrative rows cannot turn productive primary work into a two-cycle stall
            pytest.param(
                _ledger(
                    _cycle(1, True),
                    _auxiliary(_cycle(2, False)),
                    _auxiliary(_cycle(3, False)),
                    _cycle(4, True),
                    _cycle(5, True),
                ),
                id="auxiliary-rows-ignored",
            ),
            # observed primary progress starts a new failed-attempt sequence
            pytest.param(_diagnostic_sequence(), id="progress-restarts-failed-sequence"),
            # two observed attempts plus setup evidence are not three attempts
            pytest.param(
                _ledger(_cycle(1, True), _auxiliary(_cycle(2, False), evidence=["receipt repair"]), _cycle(3, True)),
                id="auxiliary-not-an-attempt",
            ),
            # a useful but unfinished recovery resumes working, before and after another productive cycle
            pytest.param(
                _recovering(
                    _cycle(1, False),
                    _cycle(2, False),
                    _cycle(3, True) | {"operation": "run one diagnostic"},
                    recovery={"action": "run one diagnostic", "material_progress": True, "closure_met": False},
                ),
                id="unfinished-recovery-resumes",
            ),
            pytest.param(
                _recovering(
                    _cycle(1, False),
                    _cycle(2, False),
                    _cycle(3, True) | {"operation": "run one diagnostic"},
                    _cycle(4, True),
                    recovery={"action": "run one diagnostic", "material_progress": True, "closure_met": False},
                ),
                id="unfinished-recovery-continues",
            ),
            # recovery history imposes no stricter threshold on a later ordinary failure
            pytest.param(_recovering(_cycle(1, True), _cycle(2, False)), id="one-failure-after-recovery"),
            # the recovery outcome survives later work that closes or genuinely stalls
            pytest.param(
                _recovering(
                    _cycle(1, True),
                    _cycle(2, True),
                    closure_condition={"id": "acceptance-check", "status": "closed"},
                    outcome="closed",
                ),
                id="recovery-then-closed",
            ),
            pytest.param(
                _recovering(
                    _cycle(1, True),
                    _cycle(2, False),
                    _cycle(3, False),
                    human_handoff=_handoff(),
                    outcome="human_handoff",
                ),
                id="recovery-then-handoff",
            ),
            # a failed recovery is acceptable once a complete human handoff is recorded
            pytest.param(
                _recovering(
                    _cycle(1, False),
                    _cycle(2, False),
                    recovery={"action": "run one diagnostic", "material_progress": False, "closure_met": False},
                    outcome="human_handoff",
                    human_handoff=_handoff(),
                ),
                id="failed-recovery-handed-off",
            ),
            # a replacement backed by a recorded decision or an observed state change keeps work authorized
            pytest.param(_replaced(source="user-direction"), id="replacement-user-direction"),
            pytest.param(_replaced(source="external-state"), id="replacement-external-state"),
        ],
    )
    def test_valid_ledger_passes(self, escalation_ledger: ModuleType, ledger: dict[str, Any]) -> None:
        """Validation returns without raising for a ledger whose progress claims match its recorded evidence.

        Each case is a different way a ledger can look stalled at a glance yet satisfy the contract.
        """
        assert escalation_ledger.validate_ledger(ledger) is None


class TestValidateLedgerRejects:
    """Ledgers that hide a stall, skip proof or use an unsupported shape fail with a named error."""

    @pytest.mark.parametrize(
        ("ledger", "error"),
        [
            # silent retries after the no-material-progress trigger
            pytest.param(_STALLED, STALL, id="stall-without-escalation"),
            # a correctly labeled auxiliary success is no progress toward the requested fix
            pytest.param(
                _ledger(_cycle(1, False), _auxiliary(_cycle(2, False)), _cycle(3, False)),
                STALL,
                id="auxiliary-keeps-stall",
            ),
            # recovery history must not loosen the three-failed-attempts bound for later cycles
            pytest.param(
                _recovering(_cycle(1, True), _cycle(2, False), _cycle(3, False)), STALL, id="failures-after-recovery"
            ),
            # report repairs must not reset a stalled implementation's counter
            pytest.param(
                _ledger(
                    _cycle(1, False),
                    _auxiliary(
                        _cycle(2, True),
                        operation="repair reviewer metadata",
                        outcome="receipt validator passes; requested code remains unchanged",
                    ),
                ),
                "auxiliary-work-is-not-primary-progress",
                id="auxiliary-claims-progress",
            ),
            # auxiliary closure conditions must not replace an omitted user objective
            pytest.param(
                {key: value for key, value in _ledger(_cycle(1, True)).items() if key != "primary_goal"},
                "primary-goal-required",
                id="missing-primary-goal",
            ),
            # active owners must upgrade old ledgers rather than weaken the current stall checks
            pytest.param(_ledger(_cycle(1, True), schema_version=1), "unsupported-schema-version", id="schema-1"),
            pytest.param(_ledger(_cycle(1, True), schema_version=2), "unsupported-schema-version", id="schema-2"),
            # a recovery boolean must be backed by the latest primary operation and evidence
            pytest.param(
                _recovering(_cycle(1, True) | {"material_progress": False}),
                "recovery-progress-evidence-required|material-progress-evidence-required",
                id="recovery-progress-unobserved",
            ),
            pytest.param(
                _recovering(_cycle(1, True), recovery=RECOVERED | {"action": "unobserved action"}),
                "recovery-progress-evidence-required|material-progress-evidence-required",
                id="recovery-operation-unobserved",
            ),
            pytest.param(
                _recovering(_cycle(1, True) | {"evidence": []}),
                "recovery-progress-evidence-required|material-progress-evidence-required",
                id="recovery-evidence-missing",
            ),
            # a bare replacement claim must not bypass the primary attempt bound
            pytest.param(
                _ledger(
                    _cycle(1, True),
                    _cycle(2, True),
                    _cycle(3, True),
                    closure_condition={"id": "acceptance-check", "status": "replaced"},
                ),
                "closure-replacement-object-required",
                id="replacement-claim-without-object",
            ),
            # a replacement needs authority and a distinct new criterion that stays observable
            pytest.param(_replaced(id=""), "closure-replacement-id-required", id="replacement-missing-new-condition"),
            pytest.param(
                _replaced(id="acceptance-check"),
                "closure-replacement-must-change-condition",
                id="replacement-unchanged",
            ),
            pytest.param(
                _replaced(source="agent-setup"), "closure-replacement-source-invalid", id="replacement-auxiliary"
            ),
            pytest.param(_replaced(evidence=[]), "closure-replacement-evidence-required", id="replacement-no-evidence"),
            pytest.param(
                _replaced(evidence=[True]), "closure-replacement-evidence-required", id="replacement-nontext-evidence"
            ),
            # an advisory without an observed read-only sandbox is not an advisory
            pytest.param(
                _ledger(
                    *_STALLED["cycles"],
                    outcome="advisory",
                    advisory=_advisory() | {"observed_sandbox": "workspace-write"},
                ),
                "advisory-read-only-sandbox-required",
                id="advisory-not-read-only",
            ),
            # no second advisor or retry after an unsuccessful bounded recovery action
            pytest.param(
                _recovering(
                    *_STALLED["cycles"],
                    recovery={"action": "run one diagnostic", "material_progress": False, "closure_met": False},
                    outcome="recovery",
                ),
                "unsuccessful-recovery-requires-human-handoff",
                id="failed-recovery-without-handoff",
            ),
        ],
    )
    def test_invalid_ledger_is_refused(self, escalation_ledger: ModuleType, ledger: dict[str, Any], error: str) -> None:
        """Validation raises the documented error code for each broken progress, escalation or proof claim.

        The error code, not just any failure, is asserted so a ledger cannot pass by tripping an unrelated rule.
        """
        with pytest.raises(ValueError, match=error):
            escalation_ledger.validate_ledger(ledger)


class TestHistoricalRead:
    """A schema-2 inline archive is readable only as history and never relaxes the current contract."""

    def test_archive_validates_when_read_as_history(self, escalation_ledger: ModuleType) -> None:
        """A schema-2 archive with inline cycles is accepted only under an explicit historical read."""
        assert escalation_ledger.validate_ledger(_ARCHIVE, historical=True) is None

    @pytest.mark.parametrize(
        ("ledger", "error"),
        [
            # the archive still meets every stall rule, so a stalled historical ledger stays invalid
            pytest.param(_STALLED | {"schema_version": 2}, STALL, id="stalled-archive"),
            # a current-schema ledger is not an archive
            pytest.param(_ledger(_cycle(1, True)), "unsupported-schema-version", id="current-schema"),
        ],
    )
    def test_historical_read_rejects(self, escalation_ledger: ModuleType, ledger: dict[str, Any], error: str) -> None:
        """A historical read accepts only the old single-file shape, not a stalled or current-schema ledger."""
        with pytest.raises(ValueError, match=error):
            escalation_ledger.validate_ledger(ledger, historical=True)


def _run_cli(ledger_path: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """Run the standalone escalation-ledger CLI as a lifecycle owner does."""
    command = [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), *flags]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _stage(run: Path, cycle: dict[str, Any]) -> None:
    """Stage one work cycle beside the header with the file tool."""
    (run / "reasoning-cycles.jsonl.rec").write_text(json.dumps(cycle, indent=2), encoding="utf-8")


def _write_header(path: Path) -> None:
    """Write a current schema-3 header holding every field except the appended cycles."""
    header = {key: value for key, value in _ledger().items() if key != "cycles"}
    path.write_text(json.dumps(header), encoding="utf-8", newline="\n")


class TestCli:
    """The standalone script appends staged cycles to a log and reads only the declared ledger shape."""

    def test_append_records_cycles_without_rewriting_earlier_lines(
        self, escalation_ledger: ModuleType, tmp_path: Path
    ) -> None:
        """Grow the cycle log one staged cycle at a time and validate the assembled ledger.

        The second cycle stalls the workstream: it is still appended as an observed fact, the CLI then fails until the
        header records the escalation, and the first cycle's line stays byte-identical throughout.
        """
        ledger_path = tmp_path / "reasoning-progress.json"
        _write_header(ledger_path)
        _stage(tmp_path, _cycle(1, False))
        first = _run_cli(ledger_path, "--append")
        first_line = (tmp_path / "reasoning-cycles.jsonl").read_bytes()
        _stage(tmp_path, _cycle(2, False))
        stalled = _run_cli(ledger_path, "--append")
        header = json.loads(ledger_path.read_text(encoding="utf-8")) | {"outcome": "advisory", "advisory": _advisory()}
        ledger_path.write_text(json.dumps(header), encoding="utf-8", newline="\n")
        escalated = _run_cli(ledger_path)

        log = (tmp_path / "reasoning-cycles.jsonl").read_bytes()
        assert (first.returncode, first.stdout.strip()) == (0, "escalation-ledger-valid")
        assert (stalled.returncode, stalled.stdout.strip()) == (2, f"escalation-ledger-invalid:{STALL}")
        assert (escalated.returncode, escalated.stdout.strip()) == (0, "escalation-ledger-valid")
        assert log.startswith(first_line)
        assert len(log.splitlines()) == 2
        assert not (tmp_path / "reasoning-cycles.jsonl.rec").exists()
        assert escalation_ledger.load_ledger(ledger_path)["cycles"] == [_cycle(1, False), _cycle(2, False)]

    @pytest.mark.parametrize(
        ("staged", "error"),
        [
            pytest.param(_cycle(3, True), "cycle-index-must-be-contiguous", id="skipped-index"),
            pytest.param(_cycle(2, True) | {"evidence": []}, "material-progress-evidence-required", id="unproven"),
        ],
    )
    def test_append_refuses_invalid_cycle_and_keeps_staged_record(
        self, tmp_path: Path, staged: dict[str, Any], error: str
    ) -> None:
        """Reject a staged cycle that breaks the sequence or cycle rules, appending nothing.

        A first valid cycle is appended, then a bad second cycle is staged; the log must stay byte-identical and the
        staged record must remain so the owner can correct it.
        """
        ledger_path = tmp_path / "reasoning-progress.json"
        _write_header(ledger_path)
        _stage(tmp_path, _cycle(1, True))
        assert _run_cli(ledger_path, "--append").returncode == 0
        before = (tmp_path / "reasoning-cycles.jsonl").read_bytes()
        _stage(tmp_path, staged)

        rejected = _run_cli(ledger_path, "--append")

        assert (rejected.returncode, rejected.stdout.strip()) == (2, f"escalation-ledger-invalid:{error}")
        assert (tmp_path / "reasoning-cycles.jsonl").read_bytes() == before
        assert (tmp_path / "reasoning-cycles.jsonl.rec").is_file()

    @pytest.mark.parametrize(
        ("payload", "flags", "expected"),
        [
            pytest.param(
                _ledger(_cycle(1, True)),
                (),
                "escalation-ledger-invalid:ledger-header-inline-cycles-forbidden",
                id="inline-cycles-in-current-schema",
            ),
            pytest.param(
                _ARCHIVE, (), "escalation-ledger-invalid:unsupported-schema-version", id="archive-read-as-current"
            ),
            pytest.param(
                _ARCHIVE, ("--historical",), "escalation-ledger-historical-valid", id="archive-read-as-history"
            ),
            pytest.param(
                _ARCHIVE,
                ("--append",),
                "escalation-ledger-invalid:append-requires-current-schema",
                id="archive-append-refused",
            ),
        ],
    )
    def test_reads_only_the_declared_ledger_shape(
        self, tmp_path: Path, payload: dict[str, Any], flags: tuple[str, ...], expected: str
    ) -> None:
        """Keep one ledger shape per schema: inline cycles are an archive, never a current or growing ledger.

        A cycle is staged in every case so an append that should be refused has something it could wrongly write.
        """
        ledger_path = tmp_path / "reasoning-progress.json"
        ledger_path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
        _stage(tmp_path, _cycle(2, True))

        result = _run_cli(ledger_path, *flags)

        assert result.stdout.strip() == expected
