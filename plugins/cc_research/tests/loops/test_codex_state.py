"""Tests for ``bin/codex_state.py`` — Codex co-pilot counters for research:run."""

from __future__ import annotations

import json
from pathlib import Path

import codex_state
import pytest

_ZERO_LINE = "CODEX_ITER=0 CODEX_NOOP_STREAK=0 CODEX_DISABLED=false"
_BLOCKED_STATE = {"codex_iter": 3, "noop_streak": 1, "codex_blocked": True}
_RESUME_WITH_CODEX = "--resume program.md --codex"
_RESUME_WITHOUT_CODEX = "--resume program.md"


@pytest.fixture(autouse=True)
def session_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Session temp dir and token of a test session, so no test reads or writes a real session's lift record."""
    session_dir = tmp_path / "session-tmp"
    session_dir.mkdir()
    monkeypatch.setenv("TMPDIR", str(session_dir))
    monkeypatch.setenv("CSID", "test-session")
    return session_dir


@pytest.fixture
def lift_record(session_tmp: Path) -> Path:
    """Path of this test session's quota-stop lift record."""
    return session_tmp / "research-run-codex-lift-test-session"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Path of a ``codex-state`` file under a run dir that does not exist yet, with cwd at ``tmp_path``."""
    monkeypatch.chdir(tmp_path)
    return Path(".experiments/state/run-1/codex-state")


@pytest.fixture
def armed(capsys: pytest.CaptureFixture[str]) -> None:
    """Arm the session's lift record the way a ``/research:run --resume <program> --codex`` request does."""
    _arm(_RESUME_WITH_CODEX, capsys)


def _arm(arguments: str, capsys: pytest.CaptureFixture[str]) -> str:
    """Run ``arm`` with the invocation's argument text as the host substitutes it, and return what it printed."""
    assert codex_state.main(["arm", "--", arguments]) == 0
    return capsys.readouterr().out.strip()


def _unblock(state: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Run one ``unblock`` (exit 0 whether or not it lifts) and return everything it printed."""
    assert codex_state.main(["unblock", "--state", str(state)]) == 0
    return capsys.readouterr().out.strip()


def _bump(state: Path, outcome: str, capsys: pytest.CaptureFixture[str]) -> str:
    """Run one successful ``bump`` and return the printed counter line."""
    assert codex_state.main(["bump", "--state", str(state), "--outcome", outcome]) == 0
    return capsys.readouterr().out.strip()


def _block(state: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Run one successful ``block`` and return the printed counter line."""
    assert codex_state.main(["block", "--state", str(state)]) == 0
    return capsys.readouterr().out.strip()


def _read(state: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Run one successful ``read`` and return the printed counter line."""
    assert codex_state.main(["read", "--state", str(state)]) == 0
    return capsys.readouterr().out.strip()


def _write_state(state: Path, payload: dict[str, object]) -> None:
    """Store ``payload`` as the run's ``codex-state`` JSON, creating the run dir."""
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(payload), encoding="utf-8")


class TestRead:
    """``read`` prints the counters as ``KEY=value`` pairs the skill shell can ``eval``."""

    def test_missing_file_reads_as_zero(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A run with no state file starts a fresh budget, silently.

        The first read of a new run has nothing to load; it must print zeros without any warning on stderr. This is also
        the state of a run without ``--codex`` (bridge absent or not requested): no file, never blocked.
        """
        assert codex_state.main(["read", "--state", str(state)]) == 0

        captured = capsys.readouterr()
        assert captured.out.strip() == _ZERO_LINE
        assert captured.err == ""

    def test_reports_disabled_at_streak_two(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``CODEX_DISABLED`` is derived from the no-op streak on read.

        A stored streak of two crosses the threshold, so the printed line says disabled although the file holds only the
        two counters.
        """
        _write_state(state, {"codex_iter": 5, "noop_streak": 2})

        assert _read(state, capsys) == "CODEX_ITER=5 CODEX_NOOP_STREAK=2 CODEX_DISABLED=true"

    def test_legacy_file_without_blocked_field_reads_unblocked(
        self, state: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A state file written before the quota flag existed loads as not blocked.

        Runs started on an older plugin version resume with the two-counter file; the absent ``codex_blocked`` key means
        false, so Codex stays enabled below the no-op limit and the line carries no ``CODEX_BLOCKED`` token.
        """
        _write_state(state, {"codex_iter": 4, "noop_streak": 1})

        assert _read(state, capsys) == "CODEX_ITER=4 CODEX_NOOP_STREAK=1 CODEX_DISABLED=false"

    def test_stored_quota_block_disables_below_streak_limit(
        self, state: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A stored quota block disables Codex on its own and is named on the line.

        The no-op streak is under the limit, so only the stored flag can explain the disable; ``CODEX_BLOCKED=true``
        lets the gate tell a quota stop from a no-op streak after a compaction.
        """
        _write_state(state, {"codex_iter": 3, "noop_streak": 0, "codex_blocked": True})

        assert _read(state, capsys) == "CODEX_ITER=3 CODEX_NOOP_STREAK=0 CODEX_DISABLED=true CODEX_BLOCKED=true"

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            "[1, 2]",
            '{"codex_iter": "x"}',
            pytest.param('{"codex_iter": 1, "noop_streak": 0, "codex_blocked": "false"}', id="blocked-as-string"),
            pytest.param('{"codex_iter": 1, "noop_streak": 0, "codex_blocked": 1}', id="blocked-as-int"),
        ],
    )
    def test_malformed_file_reads_as_zero_with_warning(
        self, state: Path, capsys: pytest.CaptureFixture[str], content: str
    ) -> None:
        """Unusable content resets to 0/0 (as the former ``|| echo 0`` did) but now says so on stderr.

        Invalid JSON, a non-object value, a non-integer counter and a non-boolean quota flag are all unusable; the run
        continues on a fresh budget and the warning keeps the reset visible. A string ``"false"`` must not read as a
        truthy block.
        """
        state.parent.mkdir(parents=True)
        state.write_text(content, encoding="utf-8")

        assert codex_state.main(["read", "--state", str(state)]) == 0

        captured = capsys.readouterr()
        assert captured.out.strip() == _ZERO_LINE
        assert "unusable" in captured.err


class TestBump:
    """``bump`` records one co-pilot pass and its outcome."""

    def test_noop_then_productive_sequence(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No-ops grow the streak, a productive pass resets it, and the file keeps the printf-compatible bytes.

        Two consecutive no-ops disable the co-pilot; the following productive pass clears the streak and re-enables it.
        """
        lines = [_bump(state, outcome, capsys) for outcome in ("noop", "noop", "productive")]

        assert lines == [
            "CODEX_ITER=1 CODEX_NOOP_STREAK=1 CODEX_DISABLED=false",
            "CODEX_ITER=2 CODEX_NOOP_STREAK=2 CODEX_DISABLED=true",
            "CODEX_ITER=3 CODEX_NOOP_STREAK=0 CODEX_DISABLED=false",
        ]
        assert state.read_text(encoding="utf-8") == '{"codex_iter": 3, "noop_streak": 0}\n'

    def test_blocked_disables_and_keeps_noop_streak(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A quota stop counts the pass, stores the block and leaves the no-op streak as it was.

        One earlier no-op left a streak of 1; the quota-blocked pass must not grow it (it is not a no-op) nor reset it
        (it is not productive), yet Codex is disabled at once through the stored flag.
        """
        _bump(state, "noop", capsys)

        line = _bump(state, "blocked", capsys)

        assert line == "CODEX_ITER=2 CODEX_NOOP_STREAK=1 CODEX_DISABLED=true CODEX_BLOCKED=true"
        assert state.read_text(encoding="utf-8") == '{"codex_iter": 2, "noop_streak": 1, "codex_blocked": true}\n'

    def test_first_quota_stop_without_state_file_persists(
        self, state: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A quota stop recorded before any co-pilot pass creates the file, and a later read still sees the block.

        R7 delegation can hit the first quota fault in a run without ``--codex``, where no codex-state exists yet. The
        ``blocked`` bump must create the file so a re-entered R7 pre-check (``read``) skips after a compaction.
        """
        _bump(state, "blocked", capsys)

        line = _read(state, capsys)

        assert line == "CODEX_ITER=1 CODEX_NOOP_STREAK=0 CODEX_DISABLED=true CODEX_BLOCKED=true"

    @pytest.mark.parametrize("later", ["noop", "productive"])
    def test_quota_block_survives_later_bumps(
        self, state: Path, capsys: pytest.CaptureFixture[str], later: str
    ) -> None:
        """A stored quota block is sticky across every later bump; only ``unblock`` clears it.

        Any bump after the block (a mistaken extra pass) rewrites the file; the flag must survive the rewrite so the
        gate keeps Codex off even when a productive pass would otherwise clear the no-op streak.
        """
        _bump(state, "blocked", capsys)

        line = _bump(state, later, capsys)

        assert line.endswith("CODEX_DISABLED=true CODEX_BLOCKED=true")
        assert json.loads(state.read_text(encoding="utf-8"))["codex_blocked"] is True

    def test_noop_streak_disable_still_derived_without_block(
        self, state: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two no-ops still disable Codex through the derived streak, with no quota flag written.

        An old bridge never classifies ``quota-exhausted``, so its blocked passes are recorded as no-ops: the existing
        streak rule must keep working and the file must not gain a ``codex_blocked`` key.
        """
        _bump(state, "noop", capsys)

        line = _bump(state, "noop", capsys)

        assert line == "CODEX_ITER=2 CODEX_NOOP_STREAK=2 CODEX_DISABLED=true"
        assert "codex_blocked" not in json.loads(state.read_text(encoding="utf-8"))

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(["bump", "--outcome", "noop"], id="bump"),
            pytest.param(["block"], id="block"),
            pytest.param(["unblock"], id="unblock"),
        ],
    )
    def test_state_outside_cwd_and_tmp_is_rejected(
        self, capsys: pytest.CaptureFixture[str], command: list[str]
    ) -> None:
        """The write target is confined to the project and the session temp dir.

        A state path at the filesystem root is refused with exit 2 and nothing is written there, for every subcommand
        that can write the file.
        """
        outside = Path(Path.home().anchor) / "codex-state-outside"

        assert codex_state.main([command[0], "--state", str(outside), *command[1:]]) == 2
        assert "outside the working directory" in capsys.readouterr().err
        assert not outside.exists()

    def test_bad_outcome_is_argument_error(self, state: Path) -> None:
        """Only ``noop``, ``productive`` and ``blocked`` are outcomes; anything else exits 2."""
        assert codex_state.main(["bump", "--state", str(state), "--outcome", "maybe"]) == 2


class TestUnblock:
    """``unblock`` lifts a stop stored before this resume when the user re-approved Codex by typing ``--codex``."""

    def test_clears_stored_block_and_keeps_counters(
        self, state: Path, capsys: pytest.CaptureFixture[str], armed: None
    ) -> None:
        """The quota flag is removed while the pass count and no-op streak stay as stored.

        A user who topped up their credits resumes the run with ``--codex``; Codex is enabled again, but the cost
        ceiling (``codex_iter``) and the streak already spent are not refunded. The file returns to the two-counter
        bytes.
        """
        _write_state(state, _BLOCKED_STATE)

        printed = _unblock(state, capsys)

        assert printed.splitlines() == [
            "codex_state: quota stop lifted — this resume typed --codex",
            "CODEX_ITER=3 CODEX_NOOP_STREAK=1 CODEX_DISABLED=false",
        ]
        assert state.read_text(encoding="utf-8") == '{"codex_iter": 3, "noop_streak": 1}\n'

    @pytest.mark.parametrize("later", ["noop", "productive"])
    def test_lifted_block_stays_lifted_across_next_bump(
        self, state: Path, capsys: pytest.CaptureFixture[str], later: str
    ) -> None:
        """A bump after ``unblock`` records the pass without bringing the quota flag back.

        The stop was stored by an earlier invocation, then this resume typed ``--codex``. The lift must persist in the
        file: the first co-pilot pass after the resume rewrites the state, and an in-memory-only lift would let that
        rewrite restore the old block.
        """
        _bump(state, "blocked", capsys)
        _arm(_RESUME_WITH_CODEX, capsys)
        _unblock(state, capsys)

        line = _bump(state, later, capsys)

        assert "CODEX_BLOCKED" not in line
        assert "codex_blocked" not in json.loads(state.read_text(encoding="utf-8"))

    def test_new_quota_stop_after_unblock_blocks_again(
        self, state: Path, capsys: pytest.CaptureFixture[str], armed: None
    ) -> None:
        """A quota fault on a pass after the lift stores the block again.

        The lift is one approval, not a waiver: if the provider is still out of credits, the next harness-classified
        quota stop disables Codex for the rest of the run as before.
        """
        _write_state(state, {"codex_iter": 1, "noop_streak": 0, "codex_blocked": True})
        _unblock(state, capsys)

        line = _bump(state, "blocked", capsys)

        assert line == "CODEX_ITER=2 CODEX_NOOP_STREAK=0 CODEX_DISABLED=true CODEX_BLOCKED=true"

    def test_noop_streak_disable_survives_unblock(
        self, state: Path, capsys: pytest.CaptureFixture[str], armed: None
    ) -> None:
        """Lifting the quota flag leaves a streak-derived disable in force.

        A run that had already reached two consecutive no-ops stays off for Codex after the lift: ``unblock`` removes
        only the quota stop, never the diminishing-returns rule.
        """
        _write_state(state, {"codex_iter": 4, "noop_streak": 2, "codex_blocked": True})

        printed = _unblock(state, capsys)

        assert printed.splitlines()[-1] == "CODEX_ITER=4 CODEX_NOOP_STREAK=2 CODEX_DISABLED=true"

    def test_missing_file_is_not_created(self, state: Path, capsys: pytest.CaptureFixture[str], armed: None) -> None:
        """A resumed run that never used the co-pilot gets no state file from ``unblock``.

        The lift block runs on every resume; with no stored state there is nothing to lift, so only the zero line is
        printed and the run dir stays untouched, even when this resume typed ``--codex``.
        """
        assert codex_state.main(["unblock", "--state", str(state)]) == 0

        captured = capsys.readouterr()
        assert captured.out.strip() == _ZERO_LINE
        assert captured.err == ""
        assert not state.exists()

    def test_unblocked_file_is_left_byte_identical(
        self, state: Path, capsys: pytest.CaptureFixture[str], armed: None
    ) -> None:
        """An older or never-blocked state file is not rewritten by ``unblock``.

        Legacy two-counter files keep their exact bytes, so the lift cannot reformat or reset a budget it has no reason
        to touch.
        """
        state.parent.mkdir(parents=True)
        state.write_text('{"codex_iter":2,"noop_streak":1}', encoding="utf-8")

        printed = _unblock(state, capsys)

        assert printed == "CODEX_ITER=2 CODEX_NOOP_STREAK=1 CODEX_DISABLED=false"
        assert state.read_text(encoding="utf-8") == '{"codex_iter":2,"noop_streak":1}'

    def test_read_never_lifts_block(self, state: Path, capsys: pytest.CaptureFixture[str], armed: None) -> None:
        """Reading the state keeps the block in the file, even in a session whose resume typed ``--codex``.

        The gate ``read`` runs at every Phase 2c and before R7; only ``unblock`` may clear the flag, so a stored block
        must survive any number of reads unchanged.
        """
        _write_state(state, {"codex_iter": 3, "noop_streak": 0, "codex_blocked": True})
        before = state.read_text(encoding="utf-8")

        line = _read(state, capsys)

        assert line.endswith("CODEX_DISABLED=true CODEX_BLOCKED=true")
        assert state.read_text(encoding="utf-8") == before


class TestLiftApproval:
    """Only this resume's typed ``--codex`` approves a lift, and never of a stop recorded after that approval."""

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param(_RESUME_WITHOUT_CODEX, id="resume-without-codex"),
            pytest.param("--resume program.md --codex-later", id="flag-prefix"),
            pytest.param("--resume program.md x--codex", id="flag-suffix"),
            pytest.param("--resume program.md -- --codex", id="after-end-of-options"),
            pytest.param('--resume program.md --codex "unbalanced', id="unparsable-arguments"),
        ],
    )
    def test_arguments_without_a_typed_codex_keep_the_stop(
        self, state: Path, capsys: pytest.CaptureFixture[str], lift_record: Path, arguments: str
    ) -> None:
        """A resume whose own arguments carry no standalone ``--codex`` token lifts nothing.

        A flag restored from ``state.json`` never appears in the invocation's arguments, so it arms nothing. Look-alike
        words, a token after the end-of-options marker and text that cannot be tokenized fail closed the same way.
        """
        _write_state(state, _BLOCKED_STATE)
        before = state.read_text(encoding="utf-8")
        _arm(arguments, capsys)

        printed = _unblock(state, capsys)

        assert printed.splitlines()[0].startswith("codex_state: quota stop kept — no --codex typed with this resume")
        assert state.read_text(encoding="utf-8") == before
        assert not lift_record.exists()

    def test_unarmed_session_keeps_the_stop(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Without any ``arm`` in this session the stop stays, so a skipped arm step cannot widen the lift.

        Covers a model running the lift block when the run skill's arm step never ran, for example an inlined load of
        the resume mode file.
        """
        _write_state(state, _BLOCKED_STATE)
        before = state.read_text(encoding="utf-8")

        printed = _unblock(state, capsys)

        assert "quota stop kept" in printed.splitlines()[0]
        assert printed.splitlines()[-1].endswith("CODEX_BLOCKED=true")
        assert state.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize(
        "stop",
        [
            pytest.param(["bump", "--outcome", "blocked"], id="pass-stop"),
            pytest.param(["block"], id="seeded-stop"),
        ],
    )
    def test_stop_recorded_after_the_approval_is_kept(
        self, state: Path, capsys: pytest.CaptureFixture[str], stop: list[str]
    ) -> None:
        """A stop stored after this session's ``--codex`` approval is never lifted by that approval.

        The lift block ran late (after a compaction, before its first run): the quota fault it would clear belongs to
        this same invocation, so the user's approval predates it and the stop stands.
        """
        _arm(_RESUME_WITH_CODEX, capsys)
        assert codex_state.main([stop[0], "--state", str(state), *stop[1:]]) == 0
        capsys.readouterr()
        before = state.read_text(encoding="utf-8")

        printed = _unblock(state, capsys)

        assert printed.splitlines()[0].startswith(
            "codex_state: quota stop kept — it was recorded in this session after the --codex approval"
        )
        assert state.read_text(encoding="utf-8") == before

    def test_replayed_arm_and_lift_keep_a_later_stop(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Re-running the arm and lift blocks after a later stop cannot lift it.

        The run resumed with ``--codex`` and lifted an old stop; a later pass hit the quota again; after a compaction
        the model replays the same arm command (same arguments) and the lift block. The arm is create-only, so the
        replay keeps the spent record and the second stop stands with its counters.
        """
        _write_state(state, _BLOCKED_STATE)
        _arm(_RESUME_WITH_CODEX, capsys)
        _unblock(state, capsys)
        _bump(state, "blocked", capsys)
        _arm(_RESUME_WITH_CODEX, capsys)

        printed = _unblock(state, capsys)

        assert "recorded in this session after the --codex approval" in printed.splitlines()[0]
        assert printed.splitlines()[-1] == "CODEX_ITER=4 CODEX_NOOP_STREAK=1 CODEX_DISABLED=true CODEX_BLOCKED=true"

    @pytest.mark.parametrize("later", ["noop", "productive"])
    def test_passes_without_a_stop_keep_the_approval(
        self, state: Path, capsys: pytest.CaptureFixture[str], later: str
    ) -> None:
        """Only a recorded stop spends the approval; ordinary passes in this session leave it usable.

        The stored stop predates the resume, so it is still the one the user approved lifting, whatever passes the
        sticky flag carried meanwhile.
        """
        _write_state(state, _BLOCKED_STATE)
        _arm(_RESUME_WITH_CODEX, capsys)
        _bump(state, later, capsys)

        printed = _unblock(state, capsys)

        assert printed.splitlines()[0] == "codex_state: quota stop lifted — this resume typed --codex"

    @pytest.mark.parametrize(
        "between",
        [
            pytest.param(_RESUME_WITHOUT_CODEX, id="resume-without-codex"),
            pytest.param("", id="unsubstituted-arguments"),
        ],
    )
    def test_no_flag_arm_cannot_reset_a_spent_record(
        self, state: Path, capsys: pytest.CaptureFixture[str], lift_record: Path, between: str
    ) -> None:
        """An arm without ``--codex`` between the stop and a re-arm keeps the stop recorded after the approval.

        An arm block run from skill text the host did not substitute passes empty arguments; if a no-flag arm could
        clear the spent mark, that empty arm plus a replayed ``--codex`` arm would lift a stop from this invocation.
        """
        _arm(_RESUME_WITH_CODEX, capsys)
        _bump(state, "blocked", capsys)
        _arm(between, capsys)
        _arm(_RESUME_WITH_CODEX, capsys)

        printed = _unblock(state, capsys)

        assert "recorded in this session after the --codex approval" in printed.splitlines()[0]
        assert lift_record.read_text(encoding="utf-8") == "spent\n"

    def test_resume_without_codex_withdraws_an_earlier_approval(
        self, state: Path, capsys: pytest.CaptureFixture[str], lift_record: Path
    ) -> None:
        """A resume without ``--codex`` does not inherit an approval an earlier resume in the session armed.

        The earlier ``--codex`` resume never met a stop, so its record is still armed; the next resume of the same
        session typed no ``--codex`` and must not lift the stop stored meanwhile by another invocation's run.
        """
        _arm(_RESUME_WITH_CODEX, capsys)
        _arm(_RESUME_WITHOUT_CODEX, capsys)
        _write_state(state, _BLOCKED_STATE)

        printed = _unblock(state, capsys)

        assert printed.splitlines()[0].startswith("codex_state: quota stop kept — no --codex typed with this resume")
        assert not lift_record.exists()

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("spent\n", id="spent"),
            pytest.param("unexpected\n", id="unknown-content"),
        ],
    )
    def test_record_other_than_armed_keeps_the_stop(
        self, state: Path, capsys: pytest.CaptureFixture[str], lift_record: Path, content: str
    ) -> None:
        """Any lift record that does not read exactly ``armed`` fails closed."""
        _write_state(state, _BLOCKED_STATE)
        lift_record.write_text(content, encoding="utf-8")

        printed = _unblock(state, capsys)

        assert "quota stop kept" in printed.splitlines()[0]

    def test_arm_reports_the_typed_flag(self, capsys: pytest.CaptureFixture[str], lift_record: Path) -> None:
        """``arm`` says that a typed ``--codex`` was seen and leaves an armed record for this session."""
        printed = _arm(_RESUME_WITH_CODEX, capsys)

        assert (
            printed == "codex_state: --codex typed with this resume; a stored quota stop from before it can be lifted"
        )
        assert lift_record.read_text(encoding="utf-8") == "armed\n"

    def test_arm_from_split_host_arguments(self, capsys: pytest.CaptureFixture[str], lift_record: Path) -> None:
        """A host-substituted argument text that the shell split into several words still finds the typed flag.

        ``"$ARGUMENTS"`` holding a quoted ``--keep`` value reaches the script as more than one word; the words are
        joined before tokenizing.
        """
        assert codex_state.main(["arm", "--", "--resume program.md --keep a", "b --codex"]) == 0

        assert lift_record.read_text(encoding="utf-8") == "armed\n"

    def test_invalid_session_token_never_arms(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, session_tmp: Path
    ) -> None:
        """A session token that is not a plain name cannot place a record, so nothing is armed and nothing escapes."""
        monkeypatch.setenv("CSID", "../escape")

        assert codex_state.main(["arm", "--", _RESUME_WITH_CODEX]) == 0

        assert "no usable session token" in capsys.readouterr().out
        assert list(session_tmp.iterdir()) == []


class TestBlock:
    """``block`` stores a quota stop carried in from elsewhere without counting a Codex pass."""

    def test_stores_stop_without_counting_a_pass(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A new run seeded with a stop has zero passes and Codex off.

        Sweep carries a judge quota stop into the run it starts; no co-pilot pass ran, so ``MAX_CODEX_RUNS`` keeps its
        whole budget for a later lift and the run report counts no pass.
        """
        line = _block(state, capsys)

        assert line == "CODEX_ITER=0 CODEX_NOOP_STREAK=0 CODEX_DISABLED=true CODEX_BLOCKED=true"
        assert state.read_text(encoding="utf-8") == '{"codex_iter": 0, "noop_streak": 0, "codex_blocked": true}\n'

    def test_keeps_stored_counters(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Blocking an existing state keeps its pass count and no-op streak."""
        _write_state(state, {"codex_iter": 3, "noop_streak": 1})

        line = _block(state, capsys)

        assert line == "CODEX_ITER=3 CODEX_NOOP_STREAK=1 CODEX_DISABLED=true CODEX_BLOCKED=true"

    def test_is_idempotent(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A second ``block`` leaves the stored bytes unchanged."""
        _block(state, capsys)
        before = state.read_text(encoding="utf-8")

        _block(state, capsys)

        assert state.read_text(encoding="utf-8") == before
