"""Tests for ``bin/verify_blueprint_audit.py``, the reader and the only component that deletes a log file.

The corpus under ``fixtures/audit/logs`` was built before this verifier existed, from an independent longhand
implementation of the record format, and ``expectations.json`` states what each file must produce. That ordering is the
point: a corpus derived from a finished verifier only proves the verifier reproduces itself.

Two contracts get individual attention beyond the table-driven sweep:

* **Exit code discipline** — only a record whose own hash fails verification exits 1. Torn framing is expected under
  concurrent appends and must never fail a run, or the check becomes noise the operator learns to ignore.
* **Honest classification** — ``observed-abstention`` requires an actual passthrough, ``incomplete-evidence`` must not
  fire on a healthy four-plugin file, and four closers reporting one completion is corroboration rather than an error.
  Each of those was a real defect in an earlier revision of the design.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

import verify_blueprint_audit as verifier


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "audit"
LOGS = FIXTURES / "logs"
EXPECTATIONS = json.loads((FIXTURES / "expectations.json").read_text(encoding="utf-8"))


def _probe_symlink() -> bool:
    """Return whether this process may create a symlink.

    A capability probe rather than a platform test: native Windows allows symlinks with the right privilege or in
    developer mode, and a blanket ``win32`` skip would hide a real regression on the machines that do support them.
    """
    with tempfile.TemporaryDirectory() as tmp:
        try:
            Path(tmp, "link").symlink_to(Path(tmp, "target"))
        except (OSError, NotImplementedError):
            return False
        return True


#: Whether symlink-based cases can run here, resolved once at collection time.
_CAN_SYMLINK = _probe_symlink()


def _probe_unreadable_file() -> bool:
    """Return whether a mode-000 file is actually unreadable to this process.

    A capability probe for the same reason as the symlink one: root ignores the permission bits, and native Windows
    does not honour a POSIX mode at all, so on either the file stays readable and a test asserting otherwise would
    fail for a reason that has nothing to do with the code.
    """
    with tempfile.TemporaryDirectory() as tmp:
        denied = Path(tmp, "denied")
        denied.write_text("x", encoding="utf-8")
        try:
            denied.chmod(0o000)
        except OSError:
            # Setting the mode failed, so nothing was proven about reading. Answering "unreadable" here would enable a
            # test whose premise was never established, and raising would turn one odd filesystem into a collection
            # error for every test in this file.
            return False
        try:
            denied.read_text(encoding="utf-8")
        except OSError:
            return True
        finally:
            denied.chmod(0o600)
        return False


#: Whether permission-denied cases can run here, resolved once at collection time.
_CAN_DENY_READ = _probe_unreadable_file()


class _DyingHandle:
    """A file handle that yields one line and then fails the way a bad disk does.

    A real mid-read failure needs hardware that is breaking or a mount that disappears, neither of which a test can
    arrange. Standing in for it at the handle keeps the code under test — the read loop, its guard, and what survives
    the stop — completely real.
    """

    def __init__(self, handle) -> None:
        self._handle = handle

    def __iter__(self):
        yield next(iter(self._handle))
        raise OSError(5, "Input/output error")

    def __enter__(self) -> _DyingHandle:
        return self

    def __exit__(self, *exc_info) -> None:
        self._handle.close()


#: The bucket names downstream tooling reads, written out independently of the module under test.
#: Renaming, adding or removing one is a contract change, and it must break this list before it breaks a consumer.
EXPECTED_BUCKETS = {
    "corrupt-record",
    "truncated",
    "schema-invalid",
    "same-agent-duplicate-after-row",
    "repeat-observation",
    "multi-allow",
    "observed-abstention",
    "incomplete-evidence",
    "incomplete-closure",
    "no-decision-evidence",
    "completion-unobserved",
    "in-flight-or-hard-kill",
    "dual-close",
    "unjoinable",
    "capped",
    "limit-exceeded",
}


def _report(path: Path, **kwargs) -> dict:
    """Return the per-file report dictionary for one fixture."""
    defaults = {"max_lines": verifier.DEFAULT_MAX_LINES, "nosession_max_bytes": verifier.DEFAULT_NOSESSION_MAX_BYTES}
    return verifier.verify_file(path, **{**defaults, **kwargs}).as_dict()


def _flagged(report: dict) -> dict[str, int]:
    """Return only the buckets a report actually raised."""
    return {name: report[name] for name in verifier.BUCKETS if report[name]}


def _fixture_params() -> list:
    """Return one param per corpus file."""
    return [pytest.param(name, id=name.removesuffix(".jsonl")) for name in sorted(EXPECTATIONS)]


class TestCorpus:
    """Every fixture reaches exactly the buckets the corpus declares."""

    @pytest.mark.parametrize("name", _fixture_params())
    def test_buckets_match_expectations(self, name: str) -> None:
        """Compare against the expectation written alongside the fixture, not against the verifier's own output."""
        expected = {key: value for key, value in EXPECTATIONS[name]["buckets"].items() if value}
        assert _flagged(_report(LOGS / name)) == expected

    @pytest.mark.parametrize("name", _fixture_params())
    def test_exit_code_matches_expectations(self, name: str, tmp_path: Path, capsys) -> None:
        """Only corruption fails the run; every other finding is a warning."""
        shutil.copy(LOGS / name, tmp_path / name)
        code = verifier.main(["verify", str(tmp_path)])
        capsys.readouterr()
        assert code == EXPECTATIONS[name]["exit"]

    def test_directory_argument_verifies_every_file(self, capsys) -> None:
        """A directory is verified whole; the totals are the sum of its files."""
        assert verifier.main(["verify", str(LOGS)]) == 1  # the corpus deliberately contains one corrupt record
        output = capsys.readouterr().out
        assert "corrupt-record" in output
        assert output.count("\n") > len(EXPECTATIONS)


class TestIntegrityVersusFraming:
    """A torn line and a mutated line are different findings with different consequences."""

    def test_corruption_fails_the_run(self, tmp_path: Path, capsys) -> None:
        """A record edited after it was written no longer matches its own hash."""
        source = (LOGS / "clean-single-plugin.jsonl").read_text(encoding="utf-8").splitlines()
        record = json.loads(source[1])
        record["project"] = "/somewhere/else"
        (tmp_path / "edited.jsonl").write_text("\n".join([source[0], json.dumps(record), *source[2:]]) + "\n")
        assert verifier.main(["verify", str(tmp_path)]) == 1
        assert "corrupt-record" in capsys.readouterr().out

    def test_torn_framing_is_a_warning_not_a_failure(self, tmp_path: Path, capsys) -> None:
        """Concurrent appends can tear a line; that is expected and must not fail the run.

        A checker that exits non-zero for something normal trains its operator to ignore it, which costs more than the
        finding is worth.
        """
        shutil.copy(LOGS / "truncated-final-line.jsonl", tmp_path / "torn.jsonl")
        assert verifier.main(["verify", str(tmp_path)]) == 0
        assert "truncated" in capsys.readouterr().out

    def test_hash_covers_the_record_without_its_own_hash(self) -> None:
        """The canonical form excludes ``record_hash``, matching the JavaScript writer exactly."""
        assert verifier.canonical({"a": 1, "record_hash": "x"}) == '{"a":1}'
        assert verifier.record_hash({"a": 1}) == hashlib.sha256(b'{"a":1}').hexdigest()


class TestHonestClassification:
    """Each of these fired wrongly in an earlier revision of the design."""

    def test_healthy_four_plugin_file_is_clean(self) -> None:
        """Four plugins writing four before-rows and four after-rows must raise nothing at all.

        An earlier revision compared every writing agent id, so a healthy run looked incomplete the moment different
        plugins closed different calls.
        """
        assert _flagged(_report(LOGS / "clean-four-plugin.jsonl")) == {}

    def test_four_closers_are_corroboration_not_duplication(self) -> None:
        """Several plugins reporting the same completion is expected; only a repeat from ONE writer is odd."""
        assert _report(LOGS / "clean-four-plugin.jsonl")["same-agent-duplicate-after-row"] == 0
        assert _report(LOGS / "same-agent-duplicate-after.jsonl")["same-agent-duplicate-after-row"] == 1

    def test_abstention_requires_an_actual_passthrough(self) -> None:
        """A group with no decision rows says nothing about authority and is never called an abstention."""
        zero_evidence = _report(LOGS / "no-decision-evidence.jsonl")
        assert zero_evidence["no-decision-evidence"] == 1
        assert zero_evidence["observed-abstention"] == 0
        assert _report(LOGS / "observed-abstention.jsonl")["observed-abstention"] == 1

    def test_incomplete_evidence_needs_a_richer_group_to_compare_against(self) -> None:
        """The bucket is a comparison between groups in one file, and is a heuristic about observed writers only."""
        assert _report(LOGS / "incomplete-evidence.jsonl")["incomplete-evidence"] == 1
        assert _report(LOGS / "clean-single-plugin.jsonl")["incomplete-evidence"] == 0

    def test_partial_closure_is_visible(self) -> None:
        """Four plugins deciding and one closing must not read the same as four closing.

        The corroboration claim — several plugins reporting one completion is agreement — is only worth making if a
        missing reporter shows up. What shows up is a per-call miss: a PostToolUse timeout on one call, or a completion
        row torn by a concurrent append. A plugin dark for the whole session closes nothing anywhere, so it never enters
        the comparison and reads clean — hence the two negatives here, which must stay silent.
        """
        assert _report(LOGS / "incomplete-closure.jsonl")["incomplete-closure"] == 1
        assert _report(LOGS / "clean-four-plugin.jsonl")["incomplete-closure"] == 0
        assert _report(LOGS / "clean-single-plugin.jsonl")["incomplete-closure"] == 0

    def test_a_four_plugin_shape_allow_is_clean_apart_from_multi_allow(self) -> None:
        """Cover the population the shape hook actually serves, not just the blueprint one.

        All four plugins ship the same shape module, so a shape match allows in all four. Pinning "clean" only on the
        blueprint case left the shape hook's own normal state — four simultaneous allows — untested.
        """
        assert _flagged(_report(LOGS / "four-plugin-shape-allow.jsonl")) == {"multi-allow": 1}

    def test_missing_completion_is_split_by_whether_the_session_ended(self) -> None:
        """A refusal, a deny rule and a dead closer are indistinguishable; only the session boundary separates cases."""
        assert _report(LOGS / "completion-unobserved.jsonl")["completion-unobserved"] == 1
        assert _report(LOGS / "in-flight-or-hard-kill.jsonl")["in-flight-or-hard-kill"] == 1

    def test_none_and_module_error_are_counted_apart_from_passthrough(self) -> None:
        """An absent opinion is not an abstention, and a crashed module is neither."""
        counts = _report(LOGS / "verdicts-mixed-and-module-error.jsonl")["counts"]["lane"]
        assert counts["blueprint"] == {"passthrough": 1, "module-error": 1}
        assert counts["shape"] == {"none": 1, "passthrough": 1}

    def test_unjoinable_rows_are_counted_never_dropped(self) -> None:
        """A row with a null join key is still evidence; silently discarding it would hide a host regression."""
        assert _report(LOGS / "unjoinable-tool-use-id.jsonl")["unjoinable"] == 2
        assert _report(LOGS / "_no-session.jsonl")["unjoinable"] == 2


class TestDerivedLineage:
    """The parent is derived on read; nothing stored claims it."""

    def test_rank_precedence_then_record_id(self) -> None:
        """Blueprint outranks shape; equal ranks break on the lexicographically smallest record id."""
        report = _report(LOGS / "multi-allow.jsonl")
        (parent,) = report["counts"]["parent"].values()
        rows = [json.loads(line) for line in (LOGS / "multi-allow.jsonl").read_text().splitlines() if line]
        blueprint_allows = sorted(
            row["record_id"]
            for row in rows
            if row.get("action_detail", {}).get("decision") == "allow"
            and row["action_detail"].get("lane") == "blueprint"
        )
        assert parent == blueprint_allows[0]

    def test_no_parent_without_an_allow(self) -> None:
        """A group where nothing allowed has no lineage to report."""
        assert _report(LOGS / "observed-abstention.jsonl")["counts"]["parent"] == {}


class TestSchemaConformance:
    """Non-conforming records are counted, excluded from grouping, and never fatal."""

    def test_schema_invalid_rows_are_excluded_not_fatal(self) -> None:
        """Four malformed rows are reported, the run still exits 0, and they take no part in grouping."""
        report = _report(LOGS / "schema-invalid.jsonl")
        assert report["schema-invalid"] == 4
        assert report["corrupt-record"] == 0

    @pytest.mark.parametrize(
        ("mutation", "why"),
        [
            pytest.param({"trust_level": "human"}, "human is reserved and never written", id="reserved-trust-level"),
            pytest.param({"outcome": "denied"}, "denied has no host signal behind it", id="reserved-outcome"),
            pytest.param({"prev_hash": "abc"}, "this log is not chained", id="chained-record"),
            pytest.param({"record_phase": "concurrent"}, "unused phase", id="unknown-phase"),
        ],
    )
    def test_records_outside_the_written_vocabulary_are_invalid(self, mutation: dict, why: str) -> None:
        """A record using a value this system never writes is not one of ours, whatever a wider spec permits."""
        rows = [json.loads(line) for line in (LOGS / "clean-single-plugin.jsonl").read_text().splitlines() if line]
        record = {**rows[1], **mutation}
        assert verifier.schema_findings(record), why


class TestLimits:
    """Neither limit is allowed to fail a run or to pass silently."""

    def test_line_limit_is_reported_and_not_fatal(self, tmp_path: Path, capsys) -> None:
        """Stopping early is a reported fact, never a silent truncation the reader mistakes for full coverage."""
        shutil.copy(LOGS / "clean-four-plugin.jsonl", tmp_path / "big.jsonl")
        assert verifier.main(["verify", str(tmp_path), "--max-lines", "2"]) == 0
        assert "limit-exceeded" in capsys.readouterr().out

    def test_no_session_cutoff_is_reported(self, tmp_path: Path) -> None:
        """A ``_no-session.jsonl`` at its cutoff is flagged so the underlying host regression gets investigated."""
        shutil.copy(LOGS / "_no-session.jsonl", tmp_path / "_no-session.jsonl")
        assert _report(tmp_path / "_no-session.jsonl", nosession_max_bytes=1)["capped"] == 1
        assert _report(tmp_path / "_no-session.jsonl")["capped"] == 0


class TestJsonOutput:
    """The machine-readable shape is a contract, so downstream tooling never parses prose."""

    def test_shape_is_files_totals_exit(self, capsys) -> None:
        """Exactly three top-level keys, one integer per bucket, and the count maps under ``counts``."""
        verifier.main(["verify", str(LOGS), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {"files", "totals", "exit"}
        assert payload["exit"] == 1
        # The expected bucket names are written out here rather than read from `verifier.BUCKETS`. Deriving the oracle
        # from the module under test would let a renamed, added or dropped bucket rewrite the contract and the test
        # that guards it in one edit, which is the whole failure this test exists to prevent.
        assert set(verifier.BUCKETS) == EXPECTED_BUCKETS
        for report in list(payload["files"].values()) + [payload["totals"]]:
            assert set(report) == {*EXPECTED_BUCKETS, "counts"}
            assert all(isinstance(report[bucket], int) for bucket in EXPECTED_BUCKETS)
            assert set(report["counts"]) == {"record_phase", "outcome", "trust_level", "agent_id", "lane", "parent"}

    def test_totals_are_the_sum_of_the_files(self, capsys) -> None:
        """A total that does not add up would send an operator looking in the wrong file."""
        verifier.main(["verify", str(LOGS), "--json"])
        payload = json.loads(capsys.readouterr().out)
        for bucket in verifier.BUCKETS:
            assert payload["totals"][bucket] == sum(report[bucket] for report in payload["files"].values())


class TestReaderClassification:
    """A record the reader cannot process must be classified into a bucket, never raised out of the run."""

    def test_a_stored_lone_surrogate_is_classified_not_raised(self, tmp_path: Path, capsys) -> None:
        """An unencodable record must be reported as corrupt, not abort the run.

        The writer refuses to emit a lone surrogate, so one on disk is damage. Hashing it raises ``UnicodeEncodeError``
        deep inside the read loop, which used to take down the whole verification — the one row nobody can read would
        cost every other row in every other file its report.
        """
        path = tmp_path / f"s-{'e' * 32}.jsonl"
        path.write_text('{"a":"x\\ud800","record_hash":"00"}\n', encoding="utf-8")
        assert _report(path)["corrupt-record"] == 1

    def test_an_unhashable_record_with_no_hash_field_is_still_corrupt(self, tmp_path: Path) -> None:
        """Both sides read as absent for such a record; comparing them directly would call it verified."""
        path = tmp_path / f"s-{'f' * 32}.jsonl"
        path.write_text('{"a":"x\\ud800"}\n', encoding="utf-8")
        assert _report(path)["corrupt-record"] == 1

    @pytest.mark.parametrize("field", ["session_id", "tool_use_id"])
    def test_an_unhashable_join_key_does_not_crash_the_reader(self, tmp_path: Path, field: str) -> None:
        """A list where a join key belongs would raise on use as a dict key and end the run.

        The schema check catches it first, so the bucket is ``schema-invalid`` rather than ``unjoinable`` — what matters
        is that a hash-valid record with a structurally impossible key is classified rather than raised.
        """
        record = {
            "agent_id": "cc_foundry/allow-dispatch",
            "session_id": "s",
            "tool_use_id": "t",
            "record_phase": "pre_execution",
            "action_detail": {"decision": "passthrough", "lane": "none"},
        }
        record[field] = ["not", "a", "string"]
        record["record_hash"] = verifier.record_hash(record)
        path = tmp_path / f"s-{'1' * 32}.jsonl"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert _report(path)["schema-invalid"] == 1

    def test_an_unhashable_event_field_does_not_crash_the_reader(self, tmp_path: Path) -> None:
        """``event`` reaches a membership test; an unhashable value there raises rather than classifying."""
        record = {
            "agent_id": "cc_foundry/audit-close",
            "session_id": "s",
            "tool_use_id": "t",
            "record_phase": "post_execution",
            "outcome": "success",
            "action_detail": {"status": "ok", "event": ["PostToolUse"]},
        }
        record["record_hash"] = verifier.record_hash(record)
        path = tmp_path / f"s-{'2' * 32}.jsonl"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert _report(path)["schema-invalid"] == 1


class TestTextReport:
    """The human-readable report, which carries every signal the exit code cannot."""

    def test_text_report_says_so_when_the_scan_stopped_early(self, capsys) -> None:
        """A truncated scan exits 0, so the incompleteness has to be printed or it is invisible.

        Exit 1 is reserved for a failed record hash and downstream tooling reads it that way. That leaves "clean" and
        "clean as far as I got" sharing an exit code, and only this line separating them.
        """
        verifier.main(["verify", str(LOGS / "clean-four-plugin.jsonl"), "--max-lines", "1"])
        assert "scan INCOMPLETE" in capsys.readouterr().out

        verifier.main(["verify", str(LOGS / "clean-four-plugin.jsonl")])
        assert "scan INCOMPLETE" not in capsys.readouterr().out

    def test_writer_saturation_is_not_reported_as_a_truncated_scan(self, tmp_path: Path, capsys) -> None:
        """``capped`` and ``limit-exceeded`` are different losses and must not share a sentence.

        ``capped`` says the WRITER refused to append to the shared stream; that file is still read in full, and the
        missing records never reached disk. ``limit-exceeded`` says THIS READ stopped early. Calling saturation an
        incomplete scan would send an operator looking for records the reader skipped, which is the wrong hunt.
        """
        shutil.copy(LOGS / "_no-session.jsonl", tmp_path / "_no-session.jsonl")
        verifier.main(["verify", str(tmp_path / "_no-session.jsonl"), "--nosession-max-bytes", "1"])
        out = capsys.readouterr().out
        assert "writer SATURATED" in out
        assert "scan INCOMPLETE" not in out

    def test_a_scan_that_read_nothing_does_not_just_say_clean(self, tmp_path: Path, capsys) -> None:
        """ "Clean" over zero files and "clean" over nine are the same two words about very different runs.

        An empty directory, a mistyped path that happens to exist, and a directory whose every entry was skipped all
        exit 0 with no findings. Without the count, an operator reads that as "the logs are healthy".
        """
        assert verifier.main(["verify", str(tmp_path)]) == 0
        report = capsys.readouterr().out
        assert "files read: 0" in report
        assert "nothing was read" in report

    def test_a_scan_that_read_files_says_how_many(self, tmp_path: Path, capsys) -> None:
        """The count is stated on every run, so its absence is never what distinguishes an empty scan."""
        shutil.copy(LOGS / "clean-single-plugin.jsonl", tmp_path / f"s-{'a' * 32}.jsonl")
        shutil.copy(LOGS / "clean-four-plugin.jsonl", tmp_path / f"s-{'b' * 32}.jsonl")

        assert verifier.main(["verify", str(tmp_path)]) == 0
        report = capsys.readouterr().out
        assert "files read: 2" in report
        assert "nothing was read" not in report


class TestReadPath:
    """Which files a target yields, and what happens to the ones that cannot be read."""

    def test_a_read_that_dies_partway_keeps_the_corruption_it_already_found(self, tmp_path: Path, capsys) -> None:
        """Corruption in the prefix still fails the run, even though the rest of the file was never read.

        Dropping the file whole on an I/O error is the quiet version of the crash it replaced: the records this reader
        already read and rejected go missing, and the run prints "no corrupt records" and exits 0 about them.
        """
        path = tmp_path / f"s-{'a' * 32}.jsonl"
        shutil.copy(LOGS / "corrupt-hash.jsonl", path)
        real_open = Path.open

        def open_that_dies_after_one_line(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            if self.name != path.name:
                return handle
            return _DyingHandle(handle)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Path, "open", open_that_dies_after_one_line)
            code = verifier.main(["verify", str(tmp_path)])

        captured = capsys.readouterr()
        assert code == 1, "corruption found before the read died must still fail the run"
        assert "corrupt-record" in captured.out
        assert "limit-exceeded" in captured.out, "the partial read must be visible as a read that stopped early"
        assert "Input/output error" in captured.err

    def test_a_file_that_could_not_be_opened_is_named_on_stdout_too(self, tmp_path: Path, capsys) -> None:
        """A skip only on stderr becomes a silent all-clear for anyone reading stdout or piping the report.

        The count of files read states the numerator; without the skip line beside it, a directory where one log of two
        could not be opened prints a clean verdict that is true only of the half it managed to read.
        """
        shutil.copy(LOGS / "clean-single-plugin.jsonl", tmp_path / f"s-{'a' * 32}.jsonl")
        denied = tmp_path / f"s-{'b' * 32}.jsonl"
        shutil.copy(LOGS / "clean-single-plugin.jsonl", denied)
        if not _CAN_DENY_READ:
            pytest.skip("this process can read a mode-000 file")
        denied.chmod(0o000)

        try:
            assert verifier.main(["verify", str(tmp_path)]) == 0
            report = capsys.readouterr().out
        finally:
            denied.chmod(0o600)

        assert "files read: 1" in report
        assert "files SKIPPED: 1" in report

    @pytest.mark.skipif(not _CAN_DENY_READ, reason="this process can read a mode-000 file")
    def test_a_directory_that_cannot_be_listed_is_not_reported_as_clean(self, tmp_path: Path, capsys) -> None:
        """``glob`` swallows the error, so an unlistable directory would otherwise read as an empty one.

        Exit 0 with "nothing was read" is the right answer for a directory holding no logs and the wrong one for a
        directory whose contents could not be seen at all — the logs may all be there, behind a mode this process cannot
        traverse.
        """
        closed = tmp_path / "closed"
        closed.mkdir()
        shutil.copy(LOGS / "clean-single-plugin.jsonl", closed / f"s-{'a' * 32}.jsonl")
        closed.chmod(0o000)

        try:
            code = verifier.main(["verify", str(closed)])
            captured = capsys.readouterr()
        finally:
            closed.chmod(0o700)

        assert code == 2, "a directory that cannot be listed is an unusable path, not an empty one"
        assert "cannot list" in captured.err
        assert "nothing was read" not in captured.out

    def test_a_stray_directory_does_not_become_a_false_corruption_alarm(self, tmp_path: Path, capsys) -> None:
        """``open`` raises on a directory exactly as ``unlink`` does, and an uncaught raise here exits 1.

        Exit 1 is reserved for ``corrupt-record``. A stray directory in the log dir must not produce the loudest signal
        the tool has about a record that is perfectly intact — nor stop the real file beside it being read.
        """
        shutil.copy(LOGS / "clean-single-plugin.jsonl", tmp_path / f"s-{'a' * 32}.jsonl")
        (tmp_path / f"s-{'b' * 32}.jsonl").mkdir()

        assert verifier.main(["verify", str(tmp_path)]) == 0
        assert f"s-{'a' * 32}.jsonl" in capsys.readouterr().out, "the real file must still be reported"

    @pytest.mark.skipif(not _CAN_SYMLINK, reason="this process may not create symlinks")
    def test_a_dangling_symlink_does_not_become_a_false_corruption_alarm(self, tmp_path: Path, capsys) -> None:
        """Same for a dead link: ``FileNotFoundError`` out of the read loop would read as corruption."""
        shutil.copy(LOGS / "clean-single-plugin.jsonl", tmp_path / f"s-{'a' * 32}.jsonl")
        (tmp_path / f"s-{'c' * 32}.jsonl").symlink_to(tmp_path / "gone.jsonl")

        assert verifier.main(["verify", str(tmp_path)]) == 0
        assert f"s-{'a' * 32}.jsonl" in capsys.readouterr().out

    @pytest.mark.skipif(not _CAN_DENY_READ, reason="this process can read a mode-000 file")
    def test_one_unreadable_file_does_not_abort_the_scan(self, tmp_path: Path, capsys) -> None:
        """A file this process may not open must not cost every other file its report.

        No type test can screen it: it is a regular file and not a link, so it passes ``log_files`` and raises at the
        read instead. Uncaught, that exits 1 — the code reserved for ``corrupt-record`` — about records nobody read,
        with stdout empty and the healthy log beside it never mentioned. The delete path has guarded this since it was
        written; the read path had not.
        """
        shutil.copy(LOGS / "clean-single-plugin.jsonl", tmp_path / f"s-{'a' * 32}.jsonl")
        denied = tmp_path / f"s-{'b' * 32}.jsonl"
        shutil.copy(LOGS / "clean-single-plugin.jsonl", denied)
        denied.chmod(0o000)

        try:
            assert verifier.main(["verify", str(tmp_path)]) == 0
            captured = capsys.readouterr()
        finally:
            denied.chmod(0o600)

        assert f"s-{'a' * 32}.jsonl" in captured.out, "the readable file must still be reported"
        assert "files read: 1" in captured.out
        assert f"s-{'b' * 32}.jsonl" in captured.err, "the file that was skipped must be named"

    def test_an_explicitly_named_symlink_is_scanned_not_skipped(self, tmp_path: Path, capsys) -> None:
        """Refusing a link the caller named would answer "clean" about a file nobody read.

        Reading through a symlink is harmless — that is why the read path differs from ``prune``, which refuses links
        because it deletes. Skipping it silently is the one way this function could lie.
        """
        if not _CAN_SYMLINK:
            pytest.skip("this process may not create symlinks")
        real = tmp_path / "real.jsonl"
        shutil.copy(LOGS / "corrupt-hash.jsonl", real)
        link = tmp_path / f"s-{'9' * 32}.jsonl"
        link.symlink_to(real)

        assert verifier.main(["verify", str(link)]) == 1, "a corrupt record behind a link must still be found"

    def test_directory_scan_names_what_it_skipped(self, tmp_path: Path, capsys) -> None:
        """Skipping an unreadable entry is right; skipping it silently is not."""
        shutil.copy(LOGS / "clean-single-plugin.jsonl", tmp_path / f"s-{'a' * 32}.jsonl")
        (tmp_path / f"s-{'b' * 32}.jsonl").mkdir()

        assert verifier.main(["verify", str(tmp_path)]) == 0
        assert f"s-{'b' * 32}.jsonl" in capsys.readouterr().err

    @pytest.mark.skipif(not _CAN_SYMLINK, reason="this process may not create symlinks")
    def test_a_skipped_symlink_is_reported_as_a_link_not_as_damage(self, tmp_path: Path, capsys) -> None:
        """A link to a healthy log is skipped because it is a link, and the message has to say that.

        A directory scan refuses links, but the file behind one may be perfectly intact. Calling it "not a regular file"
        sends its owner hunting for corruption that does not exist, in a file the scan never opened.
        """
        real = tmp_path / f"s-{'a' * 32}.jsonl"
        shutil.copy(LOGS / "clean-single-plugin.jsonl", real)
        (tmp_path / f"s-{'b' * 32}.jsonl").symlink_to(real)

        assert verifier.main(["verify", str(tmp_path)]) == 0
        message = capsys.readouterr().err
        assert f"s-{'b' * 32}.jsonl: symlink" in message
        assert "not a regular file" not in message


class TestPrune:
    """The only deletion in the system, and it races nothing."""

    @pytest.fixture(name="aged")
    def _aged(self, tmp_path: Path) -> dict[str, Path]:
        """Return a directory holding an old file, a kept session, a fresh file and the shared stream."""
        keep_session = "keep-this-session"
        paths = {
            "old": tmp_path / f"s-{hashlib.sha256(b'old').hexdigest()[:32]}.jsonl",
            "keep": tmp_path / f"s-{hashlib.sha256(keep_session.encode()).hexdigest()[:32]}.jsonl",
            "fresh": tmp_path / f"s-{hashlib.sha256(b'fresh').hexdigest()[:32]}.jsonl",
            "shared": tmp_path / "_no-session.jsonl",
        }
        for path in paths.values():
            shutil.copy(LOGS / "clean-single-plugin.jsonl", path)
        stale = time.time() - 40 * 86400
        for key in ("old", "keep", "shared"):
            os.utime(paths[key], (stale, stale))
        paths["session"] = keep_session
        return paths

    def test_dry_run_deletes_nothing_and_lists_what_it_would(self, aged: dict, capsys) -> None:
        """An operator must be able to see the consequence before accepting it."""
        assert verifier.main(["prune", str(aged["old"].parent), "--dry-run"]) == 0
        assert "would remove" in capsys.readouterr().out
        assert aged["old"].exists()

    def test_removes_only_over_age_session_files(self, aged: dict, capsys) -> None:
        """Age decides, and the shared stream is never pruned by age."""
        verifier.main(["prune", str(aged["old"].parent), "--keep-session", aged["session"]])
        capsys.readouterr()
        assert not aged["old"].exists()
        assert aged["keep"].exists(), "--keep-session must survive its own age"
        assert aged["fresh"].exists()
        assert aged["shared"].exists(), "a growing shared stream is a host regression to fix, not a file to rotate"

    def test_prints_every_removal(self, aged: dict, capsys) -> None:
        """Deletion is never silent."""
        verifier.main(["prune", str(aged["old"].parent)])
        output = capsys.readouterr().out
        assert str(aged["old"]) in output

    @pytest.mark.parametrize("name", ["timings.jsonl", "invocations.jsonl", "notes.txt", "s-short.jsonl"])
    def test_never_deletes_a_file_that_is_not_a_session_log(self, aged: dict, name: str, capsys) -> None:
        """A directory holding other logs loses nothing, however old they are.

        ``~/.claude/logs`` sits one path component above the default target and holds ``timings.jsonl`` and
        ``invocations.jsonl``, two unrelated append-only logs. Selecting by ``*.jsonl`` would delete both on a single
        mistyped path, so selection is by the writer's own filename shape instead.
        """
        directory = aged["old"].parent
        bystander = directory / name
        shutil.copy(LOGS / "clean-single-plugin.jsonl", bystander)
        stale = time.time() - 400 * 86400
        os.utime(bystander, (stale, stale))

        verifier.main(["prune", str(directory)])
        capsys.readouterr()
        assert bystander.exists(), f"{name} is not a session log and must never be pruned"
        assert not aged["old"].exists(), "the real session log must still be pruned"

    @pytest.mark.parametrize("name", ["timings.jsonl", "notes.txt"])
    def test_naming_a_single_foreign_file_deletes_nothing(self, aged: dict, name: str, capsys) -> None:
        """The single-file form is held to the same shape, so pointing prune at any other file is a no-op."""
        target = aged["old"].parent / name
        shutil.copy(LOGS / "clean-single-plugin.jsonl", target)
        stale = time.time() - 400 * 86400
        os.utime(target, (stale, stale))

        assert verifier.main(["prune", str(target)]) == 0
        assert "0 file(s) removed" in capsys.readouterr().out
        assert target.exists()

    def test_a_directory_named_like_a_log_is_skipped(self, aged: dict, capsys) -> None:
        """Only regular files are pruned; a directory carrying the name would reach ``unlink`` and raise."""
        directory = aged["old"].parent / f"s-{'b' * 32}.jsonl"
        directory.mkdir()
        stale = time.time() - 400 * 86400
        os.utime(directory, (stale, stale))

        assert verifier.main(["prune", str(aged["old"].parent)]) == 0
        capsys.readouterr()
        assert directory.is_dir()
        assert not aged["old"].exists(), "the real session log must still be pruned"

    @pytest.mark.skipif(not _CAN_SYMLINK, reason="this process may not create symlinks")
    def test_a_symlink_is_never_followed_or_removed(self, aged: dict, capsys) -> None:
        """A symlink would be judged on its target's age and then unlinked — deleting a link this writer never made."""
        outside = aged["old"].parent.parent / "outside.jsonl"
        shutil.copy(LOGS / "clean-single-plugin.jsonl", outside)
        stale = time.time() - 400 * 86400
        os.utime(outside, (stale, stale))
        link = aged["old"].parent / f"s-{'c' * 32}.jsonl"
        link.symlink_to(outside)

        assert verifier.main(["prune", str(aged["old"].parent)]) == 0
        capsys.readouterr()
        assert link.is_symlink()
        assert outside.exists()

    @pytest.mark.skipif(not _CAN_SYMLINK, reason="this process may not create symlinks")
    def test_one_unreadable_entry_does_not_abort_the_sweep(self, aged: dict, capsys) -> None:
        """A dangling symlink used to raise out of the loop, silently leaving every later file unpruned.

        Retention that stops at the first odd directory entry is worse than no retention: it keeps reporting success
        while the backlog grows behind whatever it tripped over.
        """
        dangling = aged["old"].parent / f"s-{'d' * 32}.jsonl"
        dangling.symlink_to(aged["old"].parent / "does-not-exist.jsonl")

        assert verifier.main(["prune", str(aged["old"].parent)]) == 0
        capsys.readouterr()
        assert not aged["old"].exists(), "a broken entry must not stop the files behind it from being pruned"

    @pytest.mark.parametrize("days", ["0", "-1"])
    def test_age_below_one_day_is_refused(self, aged: dict, days: str, capsys) -> None:
        """``--older-than 0`` reads as a no-op and would delete everything; it is rejected rather than obeyed."""
        assert verifier.main(["prune", str(aged["old"].parent), "--older-than", days]) == 2
        assert "at least 1 day" in capsys.readouterr().err
        assert aged["old"].exists()


class TestUnusablePath:
    """A path that does not exist is an operator error, not a finding."""

    def test_missing_path_exits_two(self, tmp_path: Path, capsys) -> None:
        """Exit 2 separates "cannot look" from "looked and found corruption"."""
        assert verifier.main(["verify", str(tmp_path / "absent")]) == 2
        assert verifier.main(["prune", str(tmp_path / "absent")]) == 2
        assert "no such path" in capsys.readouterr().err
