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
from dataclasses import dataclass
from pathlib import Path

import pytest
import verify_blueprint_audit as verifier
from _hook_env import _symlinks_supported

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "audit"
LOGS = FIXTURES / "logs"
EXPECTATIONS = json.loads((FIXTURES / "expectations.json").read_text(encoding="utf-8"))
CLEAN_SINGLE = "clean-single-plugin.jsonl"
CLEAN_FOUR = "clean-four-plugin.jsonl"


#: Whether symlink-based cases can run here, resolved once at collection time.
_CAN_SYMLINK = _symlinks_supported()


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
_skip_read_denial_unavailable = pytest.mark.skipif(not _CAN_DENY_READ, reason="this process can read a mode-000 file")
_skip_symlinks_unavailable = pytest.mark.skipif(not _CAN_SYMLINK, reason="this process may not create symlinks")


def _make_stray_directory(entry: Path, _dangling_target: Path) -> None:
    """Create a directory carrying a session-log filename."""
    entry.mkdir()


def _make_dangling_symlink(entry: Path, dangling_target: Path) -> None:
    """Create a symlink named like a session log whose target does not exist."""
    entry.symlink_to(dangling_target)


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

#: A hash-valid before-row whose join keys and event can be replaced to build structurally impossible records.
_BEFORE_ROW = {
    "agent_id": "cc_foundry/allow-dispatch",
    "session_id": "s",
    "tool_use_id": "t",
    "record_phase": "pre_execution",
    "action_detail": {"decision": "passthrough", "lane": "none"},
}
#: A hash-valid after-row, the counterpart of ``_BEFORE_ROW``.
_AFTER_ROW = {
    "agent_id": "cc_foundry/audit-close",
    "session_id": "s",
    "tool_use_id": "t",
    "record_phase": "post_execution",
    "outcome": "success",
    "action_detail": {"status": "ok", "event": "PostToolUse"},
}


def _session_log_name(fill: str) -> str:
    """Return a filename in the writer's session-log shape: ``s-`` plus 32 hex-like characters."""
    return f"s-{fill * 32}.jsonl"


def _install_log(directory: Path, fixture: str, fill: str) -> Path:
    """Copy a corpus fixture into ``directory`` under a session-log filename and return the copy."""
    target = directory / _session_log_name(fill)
    shutil.copy(LOGS / fixture, target)
    return target


def _write_session_log(directory: Path, text: str, fill: str = "1") -> Path:
    """Write ``text`` as a session log in ``directory`` and return its path."""
    path = directory / _session_log_name(fill)
    path.write_text(text, encoding="utf-8")
    return path


def _age(path: Path, days: int) -> None:
    """Set a file's modification time ``days`` into the past."""
    stale = time.time() - days * 86400
    os.utime(path, (stale, stale))


def _rows(name: str) -> list[dict]:
    """Return the parsed records of one corpus fixture."""
    return [json.loads(line) for line in (LOGS / name).read_text(encoding="utf-8").splitlines() if line]


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
        """Compare against the expectation written alongside the fixture, not against the verifier's own output.

        The expectation file was written independently of the verifier, so agreement is evidence and not a tautology.
        """
        expected = {key: value for key, value in EXPECTATIONS[name]["buckets"].items() if value}
        assert _flagged(_report(LOGS / name)) == expected

    @pytest.mark.parametrize("name", _fixture_params())
    def test_exit_code_matches_expectations(self, name: str, tmp_path: Path) -> None:
        """Only corruption fails the run; every other finding is a warning.

        Each fixture is verified alone in its own directory and the CLI exit code must equal the corpus declaration.
        """
        shutil.copy(LOGS / name, tmp_path / name)
        assert verifier.main(["verify", str(tmp_path)]) == EXPECTATIONS[name]["exit"]

    def test_directory_argument_verifies_every_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A directory is verified whole; the totals are the sum of its files.

        The corpus deliberately contains one corrupt record, so the run exits 1 and names the bucket.
        """
        assert verifier.main(["verify", str(LOGS)]) == 1
        output = capsys.readouterr().out
        assert "corrupt-record" in output
        assert output.count("\n") > len(EXPECTATIONS)


class TestIntegrityVersusFraming:
    """A torn line and a mutated line are different findings with different consequences."""

    def test_corruption_fails_the_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A record edited after it was written no longer matches its own hash.

        One field of a healthy record is rewritten without refreshing ``record_hash``; the run must exit 1.
        """
        source = (LOGS / CLEAN_SINGLE).read_text(encoding="utf-8").splitlines()
        record = json.loads(source[1])
        record["project"] = "/somewhere/else"
        (tmp_path / "edited.jsonl").write_text("\n".join([source[0], json.dumps(record), *source[2:]]) + "\n")
        assert verifier.main(["verify", str(tmp_path)]) == 1
        assert "corrupt-record" in capsys.readouterr().out

    def test_torn_framing_is_a_warning_not_a_failure(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Concurrent appends can tear a line; that is expected and must not fail the run.

        A checker that exits non-zero for something normal trains its operator to ignore it, which costs more than the
        finding is worth.
        """
        shutil.copy(LOGS / "truncated-final-line.jsonl", tmp_path / "torn.jsonl")
        assert verifier.main(["verify", str(tmp_path)]) == 0
        assert "truncated" in capsys.readouterr().out

    def test_hash_covers_the_record_without_its_own_hash(self) -> None:
        """The canonical form excludes ``record_hash``, matching the JavaScript writer exactly.

        The writer hashes the record before the hash field exists, so the reader must drop that field before hashing.
        """
        assert verifier.canonical({"a": 1, "record_hash": "x"}) == '{"a":1}'
        assert verifier.record_hash({"a": 1}) == hashlib.sha256(b'{"a":1}').hexdigest()


class TestHonestClassification:
    """Each of these fired wrongly in an earlier revision of the design."""

    @pytest.mark.parametrize(
        ("fixture", "bucket", "expected"),
        [
            # An earlier revision compared every writing agent id, so a healthy run looked incomplete the moment
            # different plugins closed different calls: several plugins reporting one completion is corroboration.
            pytest.param(CLEAN_FOUR, "same-agent-duplicate-after-row", 0, id="four-closers-are-corroboration"),
            pytest.param(
                "same-agent-duplicate-after.jsonl", "same-agent-duplicate-after-row", 1, id="one-writer-repeat"
            ),
            # A group with no decision rows says nothing about authority and is never called an abstention.
            pytest.param("no-decision-evidence.jsonl", "no-decision-evidence", 1, id="zero-evidence-reported"),
            pytest.param("no-decision-evidence.jsonl", "observed-abstention", 0, id="zero-evidence-not-abstention"),
            pytest.param("observed-abstention.jsonl", "observed-abstention", 1, id="actual-passthrough-abstains"),
            # The bucket compares groups inside one file and is a heuristic about observed writers only.
            pytest.param("incomplete-evidence.jsonl", "incomplete-evidence", 1, id="poorer-group-flagged"),
            pytest.param(CLEAN_SINGLE, "incomplete-evidence", 0, id="single-group-nothing-to-compare"),
            # Four plugins deciding and one closing must not read the same as four closing. A plugin dark for the whole
            # session closes nothing anywhere, so it never enters the comparison and reads clean.
            pytest.param("incomplete-closure.jsonl", "incomplete-closure", 1, id="partial-closure-visible"),
            pytest.param(CLEAN_FOUR, "incomplete-closure", 0, id="four-plugin-closure-complete"),
            pytest.param(CLEAN_SINGLE, "incomplete-closure", 0, id="single-plugin-closure-complete"),
            # A refusal, a deny rule and a dead closer are indistinguishable; only the session boundary separates cases.
            pytest.param("completion-unobserved.jsonl", "completion-unobserved", 1, id="session-ended-unobserved"),
            pytest.param("in-flight-or-hard-kill.jsonl", "in-flight-or-hard-kill", 1, id="session-open-in-flight"),
            # A row with a null join key is still evidence; silently discarding it would hide a host regression.
            pytest.param("unjoinable-tool-use-id.jsonl", "unjoinable", 2, id="null-tool-use-id-counted"),
            pytest.param("_no-session.jsonl", "unjoinable", 2, id="no-session-counted"),
        ],
    )
    def test_bucket_counts_reflect_the_evidence(self, fixture: str, bucket: str, expected: int) -> None:
        """Each fixture raises exactly the documented count in the bucket named for it.

        Every case pairs a corpus file with one bucket whose earlier, wrong behaviour is described in the comment above
        the case: the positive file proves the bucket still fires, the negative file proves it stays silent.
        """
        assert _report(LOGS / fixture)[bucket] == expected

    @pytest.mark.parametrize(
        ("fixture", "expected"),
        [
            pytest.param(CLEAN_FOUR, {}, id="four-plugin-blueprint-clean"),
            # All four plugins ship the same shape module, so a shape match allows in all four; pinning "clean" only on
            # the blueprint case left the shape hook's own normal state untested.
            pytest.param("four-plugin-shape-allow.jsonl", {"multi-allow": 1}, id="four-plugin-shape-multi-allow"),
        ],
    )
    def test_healthy_multi_plugin_files_raise_only_expected_buckets(self, fixture: str, expected: dict) -> None:
        """Four plugins writing four before-rows and four after-rows raise nothing beyond the expected buckets.

        An earlier revision compared every writing agent id, so a healthy run looked incomplete the moment different
        plugins closed different calls.
        """
        assert _flagged(_report(LOGS / fixture)) == expected

    def test_none_and_module_error_are_counted_apart_from_passthrough(self) -> None:
        """An absent opinion is not an abstention, and a crashed module is neither.

        The mixed fixture holds a passthrough, a ``none`` and a ``module-error``; each must land in its own count.
        """
        counts = _report(LOGS / "verdicts-mixed-and-module-error.jsonl")["counts"]["lane"]
        assert counts["blueprint"] == {"passthrough": 1, "module-error": 1}
        assert counts["shape"] == {"none": 1, "passthrough": 1}


class TestDerivedLineage:
    """The parent is derived on read; nothing stored claims it."""

    def test_rank_precedence_then_record_id(self) -> None:
        """Blueprint outranks shape; equal ranks break on the lexicographically smallest record id.

        The multi-allow fixture holds several allows in one group; the derived parent must be the smallest-id blueprint
        allow, computed here independently from the raw rows.
        """
        (parent,) = _report(LOGS / "multi-allow.jsonl")["counts"]["parent"].values()
        blueprint_allows = sorted(
            row["record_id"]
            for row in _rows("multi-allow.jsonl")
            if row.get("action_detail", {}).get("decision") == "allow"
            and row["action_detail"].get("lane") == "blueprint"
        )
        assert parent == blueprint_allows[0]

    def test_no_parent_without_an_allow(self) -> None:
        """A group where nothing allowed has no lineage to report.

        The abstention fixture decides nothing, so the parent map stays empty instead of inventing a lineage.
        """
        assert _report(LOGS / "observed-abstention.jsonl")["counts"]["parent"] == {}


class TestSchemaConformance:
    """Non-conforming records are counted, excluded from grouping, and never fatal."""

    @pytest.mark.parametrize(
        ("lanes", "valid"),
        [
            pytest.param(["blueprint", "shape"], True, id="required-lanes"),
            pytest.param(["blueprint", "shape", "gh-read"], True, id="optional-gh-read"),
            pytest.param(["blueprint"], False, id="missing-required-verdict"),
            pytest.param(["blueprint", "shape", "gh-read", "none"], False, id="too-many-verdicts"),
            pytest.param(["blueprint", "shape", "shape"], False, id="duplicate-third-lane"),
            pytest.param(["blueprint", "shape", "unknown"], False, id="unknown-third-lane"),
        ],
    )
    def test_optional_lane_keeps_verdict_count_and_identity_checks(self, lanes: list[str], valid: bool) -> None:
        """Only two or three distinct known verdict lanes satisfy the before-row contract.

        A healthy before-row has its verdict list replaced by each lane combination; the schema check must accept
        exactly the combinations the dispatcher can write.
        """
        record = _rows(CLEAN_SINGLE)[1]
        record["action_detail"]["verdicts"] = [{"lane": lane, "decision": "passthrough"} for lane in lanes]
        assert bool(verifier.schema_findings(record)) is not valid

    def test_schema_invalid_rows_are_excluded_not_fatal(self) -> None:
        """Four malformed rows are counted as schema-invalid and none is mistaken for a corrupt record.

        The fixture holds four rows that parse and hash correctly but break the schema; they are reported in their own
        bucket and never escalate to the corruption bucket that fails a run.
        """
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
        """A record using a value this system never writes is not one of ours, whatever a wider spec permits.

        A healthy before-row has one field overwritten with a value from the wider audit spec that this writer never
        emits; the schema check must flag it.
        """
        record = {**_rows(CLEAN_SINGLE)[1], **mutation}
        assert verifier.schema_findings(record), why


class TestLimits:
    """Neither limit is allowed to fail a run or to pass silently."""

    def test_line_limit_is_reported_and_not_fatal(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Stopping early is a reported fact, never a silent truncation the reader mistakes for full coverage.

        A two-line cap on a larger file must exit 0 and name ``limit-exceeded`` in the report.
        """
        shutil.copy(LOGS / CLEAN_FOUR, tmp_path / "big.jsonl")
        assert verifier.main(["verify", str(tmp_path), "--max-lines", "2"]) == 0
        assert "limit-exceeded" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("limit", "capped"),
        [pytest.param({"nosession_max_bytes": 1}, 1, id="below-file-size"), pytest.param({}, 0, id="default-limit")],
    )
    def test_no_session_cutoff_is_reported(self, limit: dict, capped: int) -> None:
        """A ``_no-session.jsonl`` at its cutoff is flagged so the underlying host regression gets investigated.

        The same shared-stream file is read with a one-byte cutoff, which it exceeds, and with the default, which it
        does not; only the first may raise ``capped``.
        """
        assert _report(LOGS / "_no-session.jsonl", **limit)["capped"] == capped


class TestJsonOutput:
    """The machine-readable shape is a contract, so downstream tooling never parses prose."""

    @pytest.fixture(name="payload")
    def _payload(self, capsys: pytest.CaptureFixture[str]) -> dict:
        """Return the parsed ``verify --json`` output for the whole corpus."""
        verifier.main(["verify", str(LOGS), "--json"])
        return json.loads(capsys.readouterr().out)

    def test_top_level_keys_and_exit(self, payload: dict) -> None:
        """Exactly three top-level keys, and the exit field mirrors the corpus's single corrupt record.

        Downstream tooling reads ``files``, ``totals`` and ``exit`` by name, so adding or renaming a key breaks it.
        """
        assert set(payload) == {"files", "totals", "exit"}
        assert payload["exit"] == 1

    def test_bucket_names_are_the_documented_set(self) -> None:
        """The bucket names are written out here rather than read from ``verifier.BUCKETS``.

        Deriving the oracle from the module under test would let a renamed, added or dropped bucket rewrite the contract
        and the test that guards it in one edit, which is the whole failure this test exists to prevent.
        """
        assert set(verifier.BUCKETS) == EXPECTED_BUCKETS

    def test_every_report_has_one_integer_per_bucket_and_the_count_maps(self, payload: dict) -> None:
        """Each file report and the totals hold one integer per bucket plus the named count maps under ``counts``.

        The same shape applies to every per-file report and to the totals, so a consumer can treat them uniformly.
        """
        reports = [*payload["files"].values(), payload["totals"]]
        assert all(set(report) == {*EXPECTED_BUCKETS, "counts"} for report in reports)
        assert all(isinstance(report[bucket], int) for report in reports for bucket in EXPECTED_BUCKETS)
        counts_keys = {"record_phase", "outcome", "trust_level", "agent_id", "lane", "parent"}
        assert all(set(report["counts"]) == counts_keys for report in reports)

    def test_totals_are_the_sum_of_the_files(self, payload: dict) -> None:
        """A total that does not add up would send an operator looking in the wrong file.

        Every bucket total is recomputed from the per-file reports in the same payload.
        """
        assert all(
            payload["totals"][bucket] == sum(report[bucket] for report in payload["files"].values())
            for bucket in verifier.BUCKETS
        )


class TestReaderClassification:
    """A record the reader cannot process must be classified into a bucket, never raised out of the run."""

    @pytest.mark.parametrize(
        "content",
        [
            # The writer refuses to emit a lone surrogate, so one on disk is damage. Hashing it raises
            # ``UnicodeEncodeError`` deep inside the read loop, which used to take down the whole verification.
            pytest.param('{"a":"x\\ud800","record_hash":"00"}\n', id="stored-lone-surrogate"),
            # Both sides read as absent for such a record; comparing them directly would call it verified.
            pytest.param('{"a":"x\\ud800"}\n', id="unhashable-without-hash-field"),
        ],
    )
    def test_an_unencodable_record_is_classified_corrupt_not_raised(self, tmp_path: Path, content: str) -> None:
        """An unencodable record is reported as corrupt instead of aborting the run.

        The one row nobody can read must not cost every other row in every other file its report, whether the record
        stores a hash (which cannot be verified) or has none at all.
        """
        assert _report(_write_session_log(tmp_path, content))["corrupt-record"] == 1

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param({**_BEFORE_ROW, "session_id": ["not", "a", "string"]}, id="unhashable-session-id"),
            pytest.param({**_BEFORE_ROW, "tool_use_id": ["not", "a", "string"]}, id="unhashable-tool-use-id"),
            pytest.param(
                {**_AFTER_ROW, "action_detail": {"status": "ok", "event": ["PostToolUse"]}}, id="unhashable-event"
            ),
        ],
    )
    def test_an_unhashable_field_does_not_crash_the_reader(self, tmp_path: Path, record: dict) -> None:
        """A list where a join key or event belongs would raise on use as a dict key or set member and end the run.

        The schema check catches it first, so the bucket is ``schema-invalid`` rather than ``unjoinable`` — what matters
        is that a hash-valid record with a structurally impossible field is classified rather than raised.
        """
        record = {**record, "record_hash": verifier.record_hash(record)}
        assert _report(_write_session_log(tmp_path, json.dumps(record) + "\n"))["schema-invalid"] == 1

    def test_a_line_nested_past_the_recursion_limit_is_torn_framing_not_raised(self, tmp_path: Path) -> None:
        """``json.loads`` raises ``RecursionError`` (not ``ValueError``) on a deeply nested line.

        A hostile or damaged audit log with one such line must cost only that line, not the whole verification run.
        """
        assert _report(_write_session_log(tmp_path, "[" * 200_000 + "\n"))["truncated"] == 1


class TestTextReport:
    """The human-readable report, which carries every signal the exit code cannot."""

    @pytest.mark.parametrize(
        ("extra_args", "incomplete"),
        [pytest.param(["--max-lines", "1"], True, id="line-limit-hit"), pytest.param([], False, id="full-scan")],
    )
    def test_text_report_says_so_only_when_the_scan_stopped_early(
        self, capsys: pytest.CaptureFixture[str], extra_args: list[str], incomplete: bool
    ) -> None:
        """A truncated scan exits 0, so the incompleteness has to be printed or it is invisible.

        Exit 1 is reserved for a failed record hash and downstream tooling reads it that way. That leaves "clean" and
        "clean as far as I got" sharing an exit code, and only this line separating them.
        """
        verifier.main(["verify", str(LOGS / CLEAN_FOUR), *extra_args])
        assert ("scan INCOMPLETE" in capsys.readouterr().out) is incomplete

    def test_writer_saturation_is_not_reported_as_a_truncated_scan(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
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

    def test_a_scan_that_read_nothing_does_not_just_say_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """ "Clean" over zero files and "clean" over nine are the same two words about very different runs.

        An empty directory, a mistyped path that happens to exist, and a directory whose every entry was skipped all
        exit 0 with no findings. Without the count, an operator reads that as "the logs are healthy".
        """
        assert verifier.main(["verify", str(tmp_path)]) == 0
        report = capsys.readouterr().out
        assert "files read: 0" in report
        assert "nothing was read" in report

    def test_a_scan_that_read_files_says_how_many(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The count is stated on every run, so its absence is never what distinguishes an empty scan.

        Two session logs are placed in an otherwise empty directory; the report must state both and must not claim
        nothing was read.
        """
        _install_log(tmp_path, CLEAN_SINGLE, "a")
        _install_log(tmp_path, CLEAN_FOUR, "b")

        assert verifier.main(["verify", str(tmp_path)]) == 0
        report = capsys.readouterr().out
        assert "files read: 2" in report
        assert "nothing was read" not in report


class TestReadPath:
    """Which files a target yields, and what happens to the ones that cannot be read."""

    @pytest.fixture(name="denied_log_dir")
    def _denied_log_dir(self, tmp_path: Path):
        """Yield a directory holding one readable session log and one with mode 000, restoring the mode on teardown."""
        _install_log(tmp_path, CLEAN_SINGLE, "a")
        denied = _install_log(tmp_path, CLEAN_SINGLE, "b")
        denied.chmod(0o000)
        yield tmp_path
        denied.chmod(0o600)

    def test_a_read_that_dies_partway_keeps_the_corruption_it_already_found(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corruption in the prefix still fails the run, even though the rest of the file was never read.

        Dropping the file whole on an I/O error is the quiet version of the crash it replaced: the records this reader
        already read and rejected go missing, and the run prints "no corrupt records" and exits 0 about them.
        """
        path = _install_log(tmp_path, "corrupt-hash.jsonl", "a")
        real_open = Path.open

        def open_that_dies_after_one_line(self, *args, **kwargs):
            """Open normally, except that the file under test reads one line and then raises an I/O error."""
            handle = real_open(self, *args, **kwargs)
            return _DyingHandle(handle) if self.name == path.name else handle

        monkeypatch.setattr(Path, "open", open_that_dies_after_one_line)

        code = verifier.main(["verify", str(tmp_path)])

        captured = capsys.readouterr()
        assert code == 1, "corruption found before the read died must still fail the run"
        assert "corrupt-record" in captured.out
        assert "limit-exceeded" in captured.out, "the partial read must be visible as a read that stopped early"
        assert "Input/output error" in captured.err

    @_skip_read_denial_unavailable
    def test_an_unreadable_file_is_skipped_and_named_without_aborting_the_scan(
        self, denied_log_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file this process may not open is named on both streams and costs no other file its report.

        A skip only on stderr becomes a silent all-clear for anyone reading stdout or piping the report, and an uncaught
        open error exits 1 — the code reserved for ``corrupt-record`` — about records nobody read, with the healthy log
        beside it never mentioned. No type test can screen the
        file:
        it is regular and not a link, so it
        passes ``log_files`` and raises at the read instead.
        """
        assert verifier.main(["verify", str(denied_log_dir)]) == 0
        captured = capsys.readouterr()
        assert _session_log_name("a") in captured.out, "the readable file must still be reported"
        assert "files read: 1" in captured.out
        assert "files SKIPPED: 1" in captured.out, "the skip must be visible on stdout, not only stderr"
        assert _session_log_name("b") in captured.err, "the file that was skipped must be named"

    @_skip_read_denial_unavailable
    def test_a_directory_that_cannot_be_listed_is_not_reported_as_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``glob`` swallows the error, so an unlistable directory would otherwise read as an empty one.

        Exit 0 with "nothing was read" is the right answer for a directory holding no logs and the wrong one for a
        directory whose contents could not be seen at all — the logs may all be there, behind a mode this process cannot
        traverse.
        """
        closed = tmp_path / "closed"
        closed.mkdir()
        _install_log(closed, CLEAN_SINGLE, "a")
        closed.chmod(0o000)
        try:
            code = verifier.main(["verify", str(closed)])
        finally:
            closed.chmod(0o700)
        captured = capsys.readouterr()

        assert code == 2, "a directory that cannot be listed is an unusable path, not an empty one"
        assert "cannot list" in captured.err
        assert "nothing was read" not in captured.out

    @pytest.mark.parametrize(
        "make_entry",
        [
            # ``open`` raises on a directory exactly as ``unlink`` does, and an uncaught raise exits 1.
            pytest.param(_make_stray_directory, id="stray-directory"),
            # ``FileNotFoundError`` out of the read loop would likewise read as corruption.
            pytest.param(_make_dangling_symlink, id="dangling-symlink", marks=_skip_symlinks_unavailable),
        ],
    )
    def test_an_unreadable_entry_is_skipped_and_named_not_reported_as_corruption(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], make_entry
    ) -> None:
        """A stray directory or dead link in the log dir is skipped by name, never a false corruption alarm.

        Exit 1 is reserved for ``corrupt-record``. An entry that cannot be read must not produce the loudest signal the
        tool has about a record that is perfectly intact, nor stop the real file beside it being read — and skipping it
        is right only if the skip is announced rather than silent.
        """
        _install_log(tmp_path, CLEAN_SINGLE, "a")
        make_entry(tmp_path / _session_log_name("b"), tmp_path / "gone.jsonl")

        assert verifier.main(["verify", str(tmp_path)]) == 0
        captured = capsys.readouterr()
        assert _session_log_name("a") in captured.out, "the real file must still be reported"
        assert _session_log_name("b") in captured.err, "the skipped entry must be named"

    @_skip_symlinks_unavailable
    def test_an_explicitly_named_symlink_is_scanned_not_skipped(self, tmp_path: Path) -> None:
        """Refusing a link the caller named would answer "clean" about a file nobody read.

        Reading through a symlink is harmless — that is why the read path differs from ``prune``, which refuses links
        because it deletes. Skipping it silently is the one way this function could lie.
        """
        real = tmp_path / "real.jsonl"
        shutil.copy(LOGS / "corrupt-hash.jsonl", real)
        link = tmp_path / _session_log_name("9")
        link.symlink_to(real)

        assert verifier.main(["verify", str(link)]) == 1, "a corrupt record behind a link must still be found"

    @_skip_symlinks_unavailable
    def test_a_skipped_symlink_is_reported_as_a_link_not_as_damage(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A link to a healthy log is skipped because it is a link, and the message has to say that.

        A directory scan refuses links, but the file behind one may be perfectly intact. Calling it "not a regular file"
        sends its owner hunting for corruption that does not exist, in a file the scan never opened.
        """
        real = _install_log(tmp_path, CLEAN_SINGLE, "a")
        (tmp_path / _session_log_name("b")).symlink_to(real)

        assert verifier.main(["verify", str(tmp_path)]) == 0
        message = capsys.readouterr().err
        assert f"{_session_log_name('b')}: symlink" in message
        assert "not a regular file" not in message


@dataclass(frozen=True)
class _AgedLogs:
    """A log directory holding an old file, a kept session, a fresh file and the shared stream."""

    directory: Path
    old: Path
    keep: Path
    fresh: Path
    shared: Path
    keep_session: str


class TestPrune:
    """The only deletion in the system, and it races nothing."""

    @pytest.fixture(name="aged")
    def _aged(self, tmp_path: Path) -> _AgedLogs:
        """Return a directory holding an old file, a kept session, a fresh file and the shared stream."""
        keep_session = "keep-this-session"
        aged = _AgedLogs(
            directory=tmp_path,
            old=tmp_path / f"s-{hashlib.sha256(b'old').hexdigest()[:32]}.jsonl",
            keep=tmp_path / f"s-{hashlib.sha256(keep_session.encode()).hexdigest()[:32]}.jsonl",
            fresh=tmp_path / f"s-{hashlib.sha256(b'fresh').hexdigest()[:32]}.jsonl",
            shared=tmp_path / "_no-session.jsonl",
            keep_session=keep_session,
        )
        for path in (aged.old, aged.keep, aged.fresh, aged.shared):
            shutil.copy(LOGS / CLEAN_SINGLE, path)
        for path in (aged.old, aged.keep, aged.shared):
            _age(path, 40)
        return aged

    def test_dry_run_deletes_nothing_and_lists_what_it_would(
        self, aged: _AgedLogs, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An operator must be able to see the consequence before accepting it.

        The over-age file is listed as "would remove" and still exists afterwards.
        """
        assert verifier.main(["prune", str(aged.directory), "--dry-run"]) == 0
        assert "would remove" in capsys.readouterr().out
        assert aged.old.exists()

    def test_removes_only_over_age_session_files(self, aged: _AgedLogs) -> None:
        """Age decides, and the shared stream is never pruned by age.

        An old session, a kept old session, a fresh session and an old shared stream sit together: only the first is
        removed.
        """
        verifier.main(["prune", str(aged.directory), "--keep-session", aged.keep_session])
        assert not aged.old.exists()
        assert aged.keep.exists(), "--keep-session must survive its own age"
        assert aged.fresh.exists()
        assert aged.shared.exists(), "a growing shared stream is a host regression to fix, not a file to rotate"

    def test_prints_every_removal(self, aged: _AgedLogs, capsys: pytest.CaptureFixture[str]) -> None:
        """Deletion is never silent.

        The path of the removed file appears in the report.
        """
        verifier.main(["prune", str(aged.directory)])
        assert ascii(str(aged.old)) in capsys.readouterr().out

    @pytest.mark.parametrize("name", ["timings.jsonl", "invocations.jsonl", "notes.txt", "s-short.jsonl"])
    def test_never_deletes_a_file_that_is_not_a_session_log(self, aged: _AgedLogs, name: str) -> None:
        """A directory holding other logs loses nothing, however old they are.

        ``~/.claude/logs`` sits one path component above the default target and holds ``timings.jsonl`` and
        ``invocations.jsonl``, two unrelated append-only logs. Selecting by ``*.jsonl`` would delete both on a single
        mistyped path, so selection is by the writer's own filename shape instead.
        """
        bystander = aged.directory / name
        shutil.copy(LOGS / CLEAN_SINGLE, bystander)
        _age(bystander, 400)

        verifier.main(["prune", str(aged.directory)])
        assert bystander.exists(), f"{name} is not a session log and must never be pruned"
        assert not aged.old.exists(), "the real session log must still be pruned"

    @pytest.mark.parametrize("name", ["timings.jsonl", "notes.txt"])
    def test_naming_a_single_foreign_file_deletes_nothing(
        self, aged: _AgedLogs, name: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The single-file form is held to the same shape, so pointing prune at any other file is a no-op.

        An old file that is not a session log is named directly; the run reports zero removals and the file remains.
        """
        target = aged.directory / name
        shutil.copy(LOGS / CLEAN_SINGLE, target)
        _age(target, 400)

        assert verifier.main(["prune", str(target)]) == 0
        assert "0 file(s) removed" in capsys.readouterr().out
        assert target.exists()

    def test_a_directory_named_like_a_log_is_skipped(self, aged: _AgedLogs) -> None:
        """Only regular files are pruned; a directory carrying the name would reach ``unlink`` and raise.

        An old directory with a session-log name sits beside a real old log; the directory survives and the log goes.
        """
        directory = aged.directory / _session_log_name("b")
        directory.mkdir()
        _age(directory, 400)

        assert verifier.main(["prune", str(aged.directory)]) == 0
        assert directory.is_dir()
        assert not aged.old.exists(), "the real session log must still be pruned"

    @_skip_symlinks_unavailable
    def test_a_symlink_is_never_followed_or_removed(self, aged: _AgedLogs) -> None:
        """A symlink would be judged on its target's age and then unlinked — deleting a link this writer never made.

        An old file outside the log directory is linked from inside it under a session-log name; neither the link nor
        its target may be touched.
        """
        outside = aged.directory.parent / "outside.jsonl"
        shutil.copy(LOGS / CLEAN_SINGLE, outside)
        _age(outside, 400)
        link = aged.directory / _session_log_name("c")
        link.symlink_to(outside)

        assert verifier.main(["prune", str(aged.directory)]) == 0
        assert link.is_symlink()
        assert outside.exists()

    @_skip_symlinks_unavailable
    def test_one_unreadable_entry_does_not_abort_the_sweep(self, aged: _AgedLogs) -> None:
        """A dangling symlink used to raise out of the loop, silently leaving every later file unpruned.

        Retention that stops at the first odd directory entry is worse than no retention: it keeps reporting success
        while the backlog grows behind whatever it tripped over.
        """
        (aged.directory / _session_log_name("d")).symlink_to(aged.directory / "does-not-exist.jsonl")

        assert verifier.main(["prune", str(aged.directory)]) == 0
        assert not aged.old.exists(), "a broken entry must not stop the files behind it from being pruned"

    @pytest.mark.parametrize("days", ["0", "-1"])
    def test_age_below_one_day_is_refused(self, aged: _AgedLogs, days: str, capsys: pytest.CaptureFixture[str]) -> None:
        """``--older-than 0`` reads as a no-op and would delete everything; it is rejected rather than obeyed.

        The refusal exits 2, names the one-day minimum on stderr, and leaves the old file in place.
        """
        assert verifier.main(["prune", str(aged.directory), "--older-than", days]) == 2
        assert "at least 1 day" in capsys.readouterr().err
        assert aged.old.exists()


class TestUnusablePath:
    """A path that does not exist is an operator error, not a finding."""

    @pytest.mark.parametrize("command", ["verify", "prune"])
    def test_missing_path_exits_two(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str) -> None:
        """Exit 2 separates "cannot look" from "looked and found corruption".

        Both subcommands given a path that does not exist exit 2 and name it as "no such path" on stderr.
        """
        assert verifier.main([command, str(tmp_path / "absent")]) == 2
        assert "no such path" in capsys.readouterr().err
