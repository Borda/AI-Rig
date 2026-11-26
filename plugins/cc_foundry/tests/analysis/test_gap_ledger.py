"""Contract tests for the run gap ledger: record schema, staged append, carry-forward and known limits."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import gap_ledger
import pytest
from _hook_env import _symlinks_supported

_skip_without_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")


def _record(**overrides: Any) -> dict[str, Any]:
    """Build a valid gap record of one review run, with top-level keys replaced by ``overrides``."""
    record: dict[str, Any] = {
        "schema": "gap-ledger",
        "schema_version": 1,
        "id": "opencv-absent",
        "class": "environment-provisionable",
        "summary": "cv2 is not importable in the review environment",
        "owner": "workflow",
        "closable_by": "provision opencv-python in an ephemeral environment",
        "evidence": "checks/tests.stderr.txt",
        "subject": {"repo": "octo/demo", "pr": 7, "head": "a1b2c3d"},
        "run": {
            "run_id": "run-001",
            "kind": "review",
            "harness": "codex",
            "plugin": "codex-rig",
            "plugin_version": "0.52.0",
            "route": "direct",
        },
    }
    record.update(overrides)
    return record


def _cuda_record(**overrides: Any) -> dict[str, Any]:
    """Build a valid hardware-absent record for a host without an NVIDIA GPU."""
    fields = {
        "id": "cuda-absent",
        "class": "hardware-absent",
        "owner": "host",
        "summary": "CUDA kernels not exercised",
        "closable_by": "run on a host with an NVIDIA GPU",
        "hardware_class": "cuda",
        "reason": "This host has no NVIDIA GPU, so the CUDA path cannot run.",
        "host_class": gap_ledger.host_class_fields(gap_ledger.current_host_class()),
    }
    return _record(**{**fields, **overrides})


def _write_lines(path: Path, records: list[dict[str, Any]]) -> Path:
    """Write records as JSON lines and return the path."""
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _limits_file(folder: Path, limits: list[dict[str, Any]]) -> Path:
    """Write a known-limits file for ``octo/demo`` and return its path."""
    path = folder / "known-limits.json"
    data = {"schema": "known-limits", "schema_version": 1, "repo": "octo/demo", "limits": limits}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


_CUDA_LIMIT = {
    "id": "cuda-absent",
    "class": "hardware-absent",
    "hardware_class": "cuda",
    "reason": "This host has no NVIDIA GPU, so the CUDA path cannot run.",
    "host_class": gap_ledger.host_class_fields(gap_ledger.current_host_class()),
}


class TestRecordErrors:
    """Schema validation of a single ledger record."""

    def test_accepts_a_complete_record(self) -> None:
        """A record carrying every required key with valid values has no errors."""
        assert gap_ledger.record_errors(_record()) == []

    def test_accepts_a_hardware_absent_record_with_exact_reason(self) -> None:
        """An absent hardware class with its exact reason is a valid gap."""
        assert gap_ledger.record_errors(_cuda_record()) == []

    @pytest.mark.parametrize("bad", ["[]", "null", "false", "1", '"row"'])
    @pytest.mark.parametrize("command", ["validate", "append"])
    @pytest.mark.parametrize("with_registry", [False, True])
    def test_cli_reports_non_object_rows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str, command: str, with_registry: bool
    ) -> None:
        """Validation and append diagnose non-object JSON with or without registry cross-checks."""
        ledger = tmp_path / "gaps.jsonl"
        source = ledger if command == "validate" else ledger.with_suffix(".jsonl.rec")
        source.write_text(f"{json.dumps(_record())}\n{bad}\n", encoding="utf-8")
        args = [command, str(ledger)]
        if with_registry:
            args.extend(["--known-limits", str(_limits_file(tmp_path, [_CUDA_LIMIT]))])

        assert gap_ledger.main(args) == 1
        assert f"{source}:2: record must be a JSON object" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            pytest.param({"class": "environment-inherent"}, "class must be one of", id="retired-class"),
            pytest.param({"owner": "reviewer"}, "owner must be one of", id="unknown-owner"),
            pytest.param({"extra": 1}, "unknown key 'extra'", id="unknown-key"),
            pytest.param({"evidence": "/tmp/log.txt"}, "relative POSIX path", id="absolute-evidence"),
            pytest.param({"evidence": "../other-run/log.txt"}, "relative POSIX path", id="escaping-evidence"),
            pytest.param({"evidence": "checks\\log.txt"}, "relative POSIX path", id="windows-separator"),
            pytest.param({"deduction": 0.055}, "at most two decimals", id="three-decimal-deduction"),
            pytest.param({"deduction": 1.5}, "from 0 to 1", id="deduction-above-one"),
            pytest.param({"id": "OpenCV Absent"}, "kebab-case slug", id="free-form-id"),
            pytest.param({"summary": "line one\nline two"}, "summary must be one line", id="multiline-summary"),
            pytest.param({"schema_version": 2}, "not supported", id="future-version"),
            pytest.param({"carried_from": "a/b"}, "carried_from must be", id="carried-from-path"),
            pytest.param({"status": "closed", "deduction": 0.02}, "carries no deduction", id="closing-with-deduction"),
        ],
    )
    def test_rejects_invalid_top_level_value(self, overrides: dict[str, Any], fragment: str) -> None:
        """Each invalid top-level value is named in the errors."""
        errors = gap_ledger.record_errors(_record(**overrides))
        assert any(fragment in error for error in errors), errors

    @pytest.mark.parametrize(
        ("run_overrides", "fragment"),
        [
            pytest.param({"route": "parallel"}, "run.route must be one of", id="runtime-mode-not-route"),
            pytest.param({"plugin_version": "latest"}, "release version", id="unversioned-plugin"),
            pytest.param({"kind": "audit"}, "run.kind must be one of", id="unknown-run-kind"),
            pytest.param({"harness": "opencode"}, "run.harness must be one of", id="unknown-harness"),
        ],
    )
    def test_rejects_invalid_run_identity(self, run_overrides: dict[str, Any], fragment: str) -> None:
        """The run identity carries a closed route, a release version, a known kind and harness."""
        run = {**_record()["run"], **run_overrides}
        errors = gap_ledger.record_errors(_record(run=run))
        assert any(fragment in error for error in errors), errors

    def test_rejects_zero_pull_request_number(self) -> None:
        """A pull request number is positive or the subject has none (null)."""
        errors = gap_ledger.record_errors(_record(subject={"repo": "octo/demo", "pr": 0, "head": "a1b2c3d"}))
        assert errors == ["subject.pr must be a positive integer or null"]

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            pytest.param({"hardware_class": "macos"}, "hardware_class must be one of", id="host-os-as-hardware"),
            pytest.param({"hardware_class": "opencv"}, "hardware_class must be one of", id="package-as-hardware"),
            pytest.param({"reason": ""}, "reason must be a non-empty string", id="missing-exact-reason"),
        ],
    )
    def test_hardware_absent_names_a_real_hardware_class_and_reason(
        self, overrides: dict[str, Any], fragment: str
    ) -> None:
        """Only a listed hardware class with a non-empty exact reason qualifies as absent hardware."""
        errors = gap_ledger.record_errors(_cuda_record(**overrides))
        assert any(fragment in error for error in errors), errors

    def test_hardware_fields_are_refused_on_other_classes(self) -> None:
        """A provisionable gap cannot borrow the hardware class to become an accepted limit."""
        errors = gap_ledger.record_errors(_record(hardware_class="cuda", reason="no GPU"))
        assert errors == ["hardware_class and reason belong to the hardware-absent class only"]


class TestValidateCli:
    """``validate`` over a whole ledger file."""

    def test_accepts_a_valid_ledger(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A ledger of valid records of one run exits 0 with an OK line."""
        ledger = _write_lines(tmp_path / "gaps.jsonl", [_record(), _cuda_record()])
        assert gap_ledger.main(["validate", str(ledger)]) == 0
        assert capsys.readouterr().out.startswith("OK: 2 record(s)")

    def test_rejects_records_of_two_runs_in_one_ledger(self, tmp_path: Path) -> None:
        """One ledger holds one run: a record with a different run identity is refused."""
        other_run = {**_record()["run"], "run_id": "run-002"}
        ledger = _write_lines(tmp_path / "gaps.jsonl", [_record(), _record(id="lint-not-run", run=other_run)])
        assert gap_ledger.main(["validate", str(ledger)]) == 1

    def test_rejects_carry_from_the_same_run(self, tmp_path: Path) -> None:
        """``carried_from`` naming the record's own run is refused."""
        ledger = _write_lines(tmp_path / "gaps.jsonl", [_record(carried_from="run-001")])
        assert gap_ledger.main(["validate", str(ledger)]) == 1

    def test_reports_invalid_json_with_line_number(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A broken line is named by file and line number."""
        ledger = tmp_path / "gaps.jsonl"
        ledger.write_text(json.dumps(_record()) + "\n{broken\n", encoding="utf-8")
        assert gap_ledger.main(["validate", str(ledger)]) == 1
        assert "gaps.jsonl:2: not valid JSON" in capsys.readouterr().out


class TestAppend:
    """Staged ``.rec`` records validated and appended by the schema owner."""

    def test_appends_staged_records_and_deletes_the_stage(self, tmp_path: Path) -> None:
        """A valid staged file lands in the ledger as sorted-key lines and the staged file is removed."""
        ledger = tmp_path / "gaps.jsonl"
        _write_lines(tmp_path / "gaps.jsonl.rec", [_record(), _cuda_record()])
        assert gap_ledger.main(["append", str(ledger)]) == 0
        assert [json.loads(line)["id"] for line in ledger.read_text(encoding="utf-8").splitlines()] == [
            "opencv-absent",
            "cuda-absent",
        ]
        assert not (tmp_path / "gaps.jsonl.rec").exists()

    def test_refuses_an_invalid_stage_and_keeps_it(self, tmp_path: Path) -> None:
        """An invalid staged record appends nothing and stays on disk for correction."""
        ledger = _write_lines(tmp_path / "gaps.jsonl", [_record()])
        before = ledger.read_bytes()
        _write_lines(tmp_path / "gaps.jsonl.rec", [_record(id="lint-not-run", **{"class": "environment-inherent"})])
        assert gap_ledger.main(["append", str(ledger)]) == 1
        assert ledger.read_bytes() == before
        assert (tmp_path / "gaps.jsonl.rec").exists()

    def test_appends_parallel_writers_in_name_order(self, tmp_path: Path) -> None:
        """Parallel writers' staged files append together, sorted by file name."""
        ledger = tmp_path / "gaps.jsonl"
        _write_lines(tmp_path / "gaps.jsonl.b-writer.rec", [_record(id="second")])
        _write_lines(tmp_path / "gaps.jsonl.a-writer.rec", [_record(id="first")])
        assert gap_ledger.main(["append", str(ledger)]) == 0
        assert [json.loads(line)["id"] for line in ledger.read_text(encoding="utf-8").splitlines()] == [
            "first",
            "second",
        ]

    def test_closes_an_unterminated_final_line_before_appending(self, tmp_path: Path) -> None:
        """A ledger whose last line lacks a newline gains one before the new record, so lines never merge."""
        ledger = tmp_path / "gaps.jsonl"
        ledger.write_text(json.dumps(_record()), encoding="utf-8")
        _write_lines(tmp_path / "gaps.jsonl.rec", [_record(id="lint-not-run")])
        assert gap_ledger.main(["append", str(ledger)]) == 0
        assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2

    def test_reports_nothing_staged(self, tmp_path: Path) -> None:
        """An append with no staged file is refused rather than silently succeeding."""
        assert gap_ledger.main(["append", str(tmp_path / "gaps.jsonl")]) == 1

    @_skip_without_symlinks
    def test_refuses_a_symlinked_ledger(self, tmp_path: Path) -> None:
        """A symlinked ledger is an I/O refusal, so a planted link cannot redirect the append."""
        target = _write_lines(tmp_path / "elsewhere.jsonl", [_record()])
        (tmp_path / "gaps.jsonl").symlink_to(target)
        _write_lines(tmp_path / "gaps.jsonl.rec", [_record(id="lint-not-run")])
        assert gap_ledger.main(["append", str(tmp_path / "gaps.jsonl")]) == 4


class TestCarry:
    """Carry-forward of an earlier run's gaps into a rerun on an identical head."""

    @staticmethod
    def _identity(tmp_path: Path, **run_overrides: Any) -> Path:
        """Write the new run's identity file and return its path."""
        base = _record()
        run = {**base["run"], "run_id": "run-002", **run_overrides}
        path = tmp_path / "identity.json"
        path.write_text(json.dumps({"subject": base["subject"], "run": run}), encoding="utf-8")
        return path

    def test_carries_open_gaps_with_carried_from(self, tmp_path: Path) -> None:
        """Same head and plugin version: every open gap is appended with the earlier run in ``carried_from``."""
        prior = _write_lines(tmp_path / "prior.jsonl", [_record(), _cuda_record()])
        ledger = tmp_path / "gaps.jsonl"
        args = ["carry", "--from", str(prior), "--identity", str(self._identity(tmp_path)), str(ledger)]
        assert gap_ledger.main(args) == 0
        carried = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert [(item["id"], item["carried_from"], item["run"]["run_id"]) for item in carried] == [
            ("opencv-absent", "run-001", "run-002"),
            ("cuda-absent", "run-001", "run-002"),
        ]

    def test_does_not_carry_a_gap_closed_in_the_earlier_run(self, tmp_path: Path) -> None:
        """A gap whose last event in the earlier run is ``closed`` is not carried."""
        prior = _write_lines(tmp_path / "prior.jsonl", [_record(), _record(status="closed")])
        ledger = tmp_path / "gaps.jsonl"
        args = ["carry", "--from", str(prior), "--identity", str(self._identity(tmp_path)), str(ledger)]
        assert gap_ledger.main(args) == 0
        assert not ledger.exists()

    @pytest.mark.parametrize(
        "run_overrides",
        [
            pytest.param({"plugin_version": "0.53.0"}, id="plugin-version-differs"),
            pytest.param({"kind": "remediate"}, id="run-kind-differs"),
        ],
    )
    def test_refuses_when_the_run_differs(self, tmp_path: Path, run_overrides: dict[str, Any]) -> None:
        """A different plugin version or run kind exits 3 and writes nothing."""
        prior = _write_lines(tmp_path / "prior.jsonl", [_record()])
        ledger = tmp_path / "gaps.jsonl"
        identity = self._identity(tmp_path, **run_overrides)
        assert gap_ledger.main(["carry", "--from", str(prior), "--identity", str(identity), str(ledger)]) == 3
        assert not ledger.exists()

    @pytest.mark.parametrize(
        "registry",
        [
            pytest.param(None, id="no-registry"),
            pytest.param({"repo": "other/repo", "limits": [_CUDA_LIMIT]}, id="registry-of-another-repository"),
            pytest.param({"repo": "octo/demo", "limits": []}, id="registry-entry-deleted"),
        ],
    )
    def test_known_limit_without_a_current_registry_entry_is_not_carried(
        self, tmp_path: Path, registry: dict[str, Any] | None
    ) -> None:
        """A prior known limit the new run's registry no longer lists is left for the new run to record fresh."""
        prior = _write_lines(tmp_path / "prior.jsonl", [_record(), _cuda_record(known_limit=True)])
        ledger = tmp_path / "gaps.jsonl"
        args = ["carry", "--from", str(prior), "--identity", str(self._identity(tmp_path)), str(ledger)]
        if registry is not None:
            path = tmp_path / "known-limits.json"
            path.write_text(json.dumps({"schema": "known-limits", "schema_version": 1, **registry}), encoding="utf-8")
            args += ["--known-limits", str(path)]
        assert gap_ledger.main(args) == 0
        carried = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert [item["id"] for item in carried] == ["opencv-absent"]
        assert all("known_limit" not in item for item in carried)

    def test_listed_known_limit_is_carried_with_its_flag(self, tmp_path: Path) -> None:
        """A known limit the registry of the same repository still lists is carried as a known limit."""
        prior = _write_lines(tmp_path / "prior.jsonl", [_cuda_record(known_limit=True)])
        limits = _limits_file(tmp_path, [_CUDA_LIMIT])
        ledger = tmp_path / "gaps.jsonl"
        identity = str(self._identity(tmp_path))
        args = ["carry", "--from", str(prior), "--identity", identity, str(ledger), "--known-limits", str(limits)]
        assert gap_ledger.main(args) == 0
        [carried] = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert (carried["known_limit"], carried["deduction"], carried["carried_from"]) == (True, None, "run-001")

    def test_repository_spelling_does_not_block_a_carry(self, tmp_path: Path) -> None:
        """A prior run that spelled the repository in another case is the same repository."""
        prior = _write_lines(
            tmp_path / "prior.jsonl", [_record(subject={"repo": "Octo/Demo", "pr": 7, "head": "a1b2c3d"})]
        )
        ledger = tmp_path / "gaps.jsonl"
        args = ["carry", "--from", str(prior), "--identity", str(self._identity(tmp_path)), str(ledger)]
        assert gap_ledger.main(args) == 0
        assert ledger.exists()

    def test_refuses_a_different_head(self, tmp_path: Path) -> None:
        """A new head can have opened or closed gaps, so nothing is carried (exit 3)."""
        prior = _write_lines(
            tmp_path / "prior.jsonl", [_record(subject={"repo": "octo/demo", "pr": 7, "head": "fff0001"})]
        )
        ledger = tmp_path / "gaps.jsonl"
        assert (
            gap_ledger.main(["carry", "--from", str(prior), "--identity", str(self._identity(tmp_path)), str(ledger)])
            == 3
        )
        assert not ledger.exists()


class TestKnownLimits:
    """Per-repository known-limits file and its cross-check against ledgers."""

    def test_accepts_an_absent_hardware_class(self, tmp_path: Path) -> None:
        """A limits file listing CUDA with its exact reason is valid."""
        assert gap_ledger.main(["limits", str(_limits_file(tmp_path, [_CUDA_LIMIT]))]) == 0

    @pytest.mark.parametrize(
        "limit",
        [
            pytest.param({**_CUDA_LIMIT, "id": "opencv-absent", "class": "environment-provisionable"}, id="package"),
            pytest.param({**_CUDA_LIMIT, "id": "macos-host", "class": "process"}, id="host-os"),
            pytest.param({**_CUDA_LIMIT, "reason": ""}, id="no-exact-reason"),
            pytest.param({**_CUDA_LIMIT, "hardware_class": "macos"}, id="os-as-hardware"),
        ],
    )
    def test_refuses_anything_but_an_absent_hardware_class(self, tmp_path: Path, limit: dict[str, Any]) -> None:
        """Packages, the host OS and reasonless entries never qualify as known limits."""
        assert gap_ledger.main(["limits", str(_limits_file(tmp_path, [limit]))]) == 1

    def test_refuses_a_listed_limit_that_is_deducted_again(self, tmp_path: Path) -> None:
        """A record of a listed limit must carry no deduction: it is shown once, never deducted again."""
        limits = _limits_file(tmp_path, [_CUDA_LIMIT])
        _write_lines(tmp_path / "gaps.jsonl.rec", [_cuda_record(known_limit=True, deduction=0.05)])
        assert gap_ledger.main(["append", str(tmp_path / "gaps.jsonl"), "--known-limits", str(limits)]) == 1

    def test_accepts_an_unmarked_first_observation_of_a_listed_limit(self, tmp_path: Path) -> None:
        """An observation without ``known_limit`` keeps its deduction: the limit is registered after the first one."""
        limits = _limits_file(tmp_path, [_CUDA_LIMIT])
        _write_lines(tmp_path / "gaps.jsonl.rec", [_cuda_record()])
        assert gap_ledger.main(["append", str(tmp_path / "gaps.jsonl"), "--known-limits", str(limits)]) == 0

    def test_accepts_a_listed_limit_shown_without_deduction(self, tmp_path: Path) -> None:
        """A listed limit marked ``known_limit`` with no deduction appends."""
        limits = _limits_file(tmp_path, [_CUDA_LIMIT])
        repo, listed = gap_ledger.read_known_limits(limits)
        observation = _cuda_record(reason="No NVIDIA GPU here.", deduction=0.03)
        applied = gap_ledger.apply_known_limit(observation, listed)
        assert (applied["known_limit"], applied["deduction"], applied["reason"]) == (True, None, _CUDA_LIMIT["reason"])
        assert gap_ledger.known_limit_errors([applied], repo, listed) == []
        _write_lines(tmp_path / "gaps.jsonl.rec", [applied])
        assert gap_ledger.main(["append", str(tmp_path / "gaps.jsonl"), "--known-limits", str(limits)]) == 0

    def test_provisioning_reason_cannot_be_applied_or_loaded(self, tmp_path: Path) -> None:
        """An ineligible listed reason cannot overwrite a valid device observation."""
        invalid = {**_CUDA_LIMIT, "reason": "No NVIDIA GPU on this host; the CUDA driver is not installed."}
        observation = _cuda_record(deduction=0.03)
        applied = gap_ledger.apply_known_limit(observation, [gap_ledger.KnownLimit(**_known(invalid))])
        assert applied == observation
        path = _limits_file(tmp_path, [invalid])
        with pytest.raises(gap_ledger.LedgerInvalidError, match="reason"):
            gap_ledger.read_known_limits(path)

    @pytest.mark.parametrize(
        "reason",
        ["No TPU on this host.", "No GPU on this host.", "No CUDA TPU.", "rocm-absent: no CUDA GPU."],
    )
    def test_listed_reason_must_name_its_own_device_class(self, tmp_path: Path, reason: str) -> None:
        """A malformed listed reason cannot waive a valid observation or load from disk."""
        invalid = {**_CUDA_LIMIT, "reason": reason}
        observation = _cuda_record(deduction=0.03)

        assert gap_ledger.apply_known_limit(observation, [gap_ledger.KnownLimit(**_known(invalid))]) == observation
        with pytest.raises(gap_ledger.LedgerInvalidError, match="reason"):
            gap_ledger.read_known_limits(_limits_file(tmp_path, [invalid]))

    def test_refuses_an_unlisted_known_limit_claim(self, tmp_path: Path) -> None:
        """``known_limit: true`` must name a limit the repository lists."""
        limits = _limits_file(tmp_path, [_CUDA_LIMIT])
        _write_lines(
            tmp_path / "gaps.jsonl.rec", [_cuda_record(id="rocm-absent", hardware_class="rocm", known_limit=True)]
        )
        assert gap_ledger.main(["append", str(tmp_path / "gaps.jsonl"), "--known-limits", str(limits)]) == 1

    @pytest.mark.parametrize("command", ["append", "validate"])
    def test_refuses_a_known_limit_claim_it_cannot_verify(self, tmp_path: Path, command: str) -> None:
        """Without a known-limits file a zero-deduction claim is never accepted on the record's own word."""
        claim = _cuda_record(known_limit=True, host_class={"os": "plan9", "architecture": "vax"})
        target = tmp_path / ("gaps.jsonl.rec" if command == "append" else "gaps.jsonl")
        _write_lines(target, [claim])
        assert gap_ledger.main([command, str(tmp_path / "gaps.jsonl")]) == 1

    def test_refuses_a_known_limit_claim_of_another_repository(self, tmp_path: Path) -> None:
        """A file listing another repository's limits cannot verify this repository's claim."""
        path = tmp_path / "known-limits.json"
        other = {"schema": "known-limits", "schema_version": 1, "repo": "other/repo", "limits": [_CUDA_LIMIT]}
        path.write_text(json.dumps(other), encoding="utf-8")
        _write_lines(tmp_path / "gaps.jsonl.rec", [_cuda_record(known_limit=True)])
        assert gap_ledger.main(["append", str(tmp_path / "gaps.jsonl"), "--known-limits", str(path)]) == 1

    def test_registry_matches_the_repository_case_insensitively(self, tmp_path: Path) -> None:
        """``Octo/Demo`` in the registry verifies a claim recorded for ``octo/demo``."""
        path = tmp_path / "known-limits.json"
        listed = {"schema": "known-limits", "schema_version": 1, "repo": "Octo/Demo", "limits": [_CUDA_LIMIT]}
        path.write_text(json.dumps(listed), encoding="utf-8")
        _write_lines(tmp_path / "gaps.jsonl.rec", [_cuda_record(known_limit=True)])
        assert gap_ledger.main(["append", str(tmp_path / "gaps.jsonl"), "--known-limits", str(path)]) == 0

    @pytest.mark.parametrize(
        "cause",
        [
            pytest.param(
                "CUDA tests skipped: torch CPU-only wheel installed, CUDA not available.", id="cpu-only-wheel"
            ),
            pytest.param("no CUDA-enabled torch build could be provisioned", id="build-not-provisioned"),
            pytest.param("cupy not installed, so the CUDA path did not run", id="package-not-installed"),
        ],
    )
    def test_provisioning_cause_never_matches_a_listed_limit(self, tmp_path: Path, cause: str) -> None:
        """A gap caused by a package or build matches no known limit, so it keeps its deduction."""
        record = _cuda_record(reason=cause, deduction=0.03)
        assert gap_ledger.matching_limit(record, [gap_ledger.KnownLimit(**_known(_CUDA_LIMIT))]) is None
        assert gap_ledger.apply_known_limit(record, [gap_ledger.KnownLimit(**_known(_CUDA_LIMIT))]) == record


def _known(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the ``KnownLimit`` fields of a known-limits file entry."""
    host = entry["host_class"]
    return {
        "id": entry["id"],
        "hardware_class": entry["hardware_class"],
        "reason": entry["reason"],
        "host_class": (host["os"], host["architecture"]),
    }


class TestRegistration:
    """Automatic registration of an observed absent hardware class into the repository's registry."""

    @pytest.mark.parametrize(
        "reason",
        [
            pytest.param("torch CPU-only wheel installed, CUDA not available.", id="cpu-only-wheel"),
            pytest.param("torch 2.3.0+cpu installed, CUDA not available", id="cpu-local-version"),
            pytest.param("CUDA not available: NVIDIA driver missing", id="driver-missing"),
            pytest.param("CUDA unavailable, torch installed from the CPU index", id="cpu-index"),
            pytest.param("CUDA is not available in this environment", id="stack-unavailable-no-device-statement"),
            pytest.param("No GPU tests executed on this host.", id="tests-not-run"),
            pytest.param("No NVIDIA GPU checks ran.", id="checks-not-run"),
            pytest.param("No TPU kernels executed.", id="kernels-not-run"),
            pytest.param("No XPU cases were collected.", id="cases-not-run"),
            pytest.param("Only one GPU test ran.", id="one-test-not-one-device"),
            pytest.param("No NVIDIA GPU visible because PyTorch was built for CPU.", id="build-bounds-visibility"),
            pytest.param("NVIDIA GPU not detected with the distributed binary.", id="binary-bounds-detection"),
            pytest.param("No CUDA hardware recognized by the selected package.", id="package-bounds-recognition"),
            pytest.param("No NVIDIA GPU (the wheel targets the host processor).", id="parenthetical-build-scope"),
            pytest.param(
                "NVIDIA GPU not detected (the runtime cannot enumerate it).", id="parenthetical-runtime-scope"
            ),
            pytest.param("The broken runtime reports no NVIDIA GPU.", id="software-reports-absence"),
            pytest.param("This host has no NVIDIA GPU; the CUDA driver is not installed.", id="mixed-hardware-driver"),
            pytest.param("No NVIDIA GPU, so a wheel compiled for CPU cannot run.", id="build-in-consequence"),
            pytest.param("This host has an NVIDIA GPU and lacks CUDA hardware.", id="free-text-inside-host-clause"),
        ],
    )
    def test_only_a_stated_absent_device_is_registered(self, tmp_path: Path, reason: str) -> None:
        """A provisioning cause or an unavailable software stack never becomes a permanent hardware limit."""
        path = tmp_path / ".notes" / "known-limits.json"
        record = _cuda_record(reason=reason, summary=reason, deduction=0.03)
        assert gap_ledger.register_known_limits(path, "octo/demo", [record]) == ([], None)
        assert not path.exists()

    @pytest.mark.parametrize(
        "reason",
        [
            "No accessible NVIDIA GPU.",
            "No supported NVIDIA GPU.",
            "No compatible NVIDIA GPU.",
            "NVIDIA GPU not detected.",
            "NVIDIA GPU not found.",
            "CUDA device unavailable.",
            "No CUDA device available.",
            "NVIDIA GPU not installed.",
            "No NVIDIA GPU installed.",
            "This runner has no NVIDIA GPU provisioned.",
        ],
    )
    def test_visibility_and_setup_reasons_cannot_create_or_apply_a_limit(self, tmp_path: Path, reason: str) -> None:
        """Require physical absence when registering, reading a hand-written entry, or matching a limit."""
        observation = _cuda_record(reason=reason, deduction=0.03)
        entry = {**_CUDA_LIMIT, "reason": reason}
        path = tmp_path / "known-limits.json"
        assert gap_ledger.register_known_limits(path, "octo/demo", [observation]) == ([], None)
        assert not path.exists()
        assert gap_ledger._limit_errors(entry) == [
            "reason must directly state a device absence matching hardware_class"
        ]
        with pytest.raises(gap_ledger.LedgerInvalidError, match="reason"):
            gap_ledger.read_known_limits(_limits_file(tmp_path, [entry]))
        assert gap_ledger.apply_known_limit(observation, [gap_ledger.KnownLimit(**_known(_CUDA_LIMIT))]) == observation
        valid = _cuda_record(deduction=0.03)
        assert gap_ledger.apply_known_limit(valid, [gap_ledger.KnownLimit(**_known(entry))]) == valid

    def test_repository_spelling_does_not_block_registration(self, tmp_path: Path) -> None:
        """A record of ``Octo/Demo`` registers into the registry of ``octo/demo``."""
        path = tmp_path / ".notes" / "known-limits.json"
        record = _cuda_record(subject={"repo": "Octo/Demo", "pr": 7, "head": "a1b2c3d"}, deduction=0.03)
        assert gap_ledger.register_known_limits(path, "octo/demo", [record]) == (["cuda-absent"], None)

    @pytest.mark.parametrize(
        ("hardware", "reason"),
        [
            pytest.param("cuda", "no NVIDIA GPU", id="bare-device"),
            pytest.param("cuda", "This host has no NVIDIA GPU.", id="host-ownership"),
            pytest.param("cuda", "Test runner: no CUDA GPU.", id="host-label"),
            pytest.param("rocm", "ROCm tests skipped: no AMD GPU.", id="test-label"),
            pytest.param("tensorrt", "No TensorRT hardware.", id="tensorrt"),
            pytest.param("xpu", "XPU not present on this host.", id="xpu"),
            pytest.param("tpu", "No local TPU here.", id="tpu"),
            pytest.param("npu", "No NPU on this machine.", id="npu"),
            pytest.param("mps", "No MPS device.", id="mps"),
            pytest.param("multi-gpu", "Only one GPU on this host.", id="multi-gpu"),
        ],
    )
    def test_direct_device_observation_registers_and_matches(self, tmp_path: Path, hardware: str, reason: str) -> None:
        """Register and apply a direct reason for each supported hardware class."""
        path = tmp_path / "known-limits.json"
        gap_id = f"{hardware}-absent"
        observation = _cuda_record(id=gap_id, hardware_class=hardware, reason=reason, summary=reason, deduction=0.03)

        assert gap_ledger.register_known_limits(path, "octo/demo", [observation]) == ([gap_id], None)
        repo, limits = gap_ledger.read_known_limits(path)
        applied = gap_ledger.apply_known_limit(observation, limits)
        assert (applied["known_limit"], applied["deduction"]) == (True, None)
        assert gap_ledger.known_limit_errors([applied], repo, limits) == []

    @pytest.mark.parametrize(
        "summary",
        [
            "NVIDIA GPU not detected with the distributed binary.",
            "CUDA tests skipped: the container was started without --gpus.",
            "The container cannot see the host's NVIDIA GPU.",
            "The worker was launched with an empty device list.",
            "CUDA tests skipped because the image exposes zero accelerators.",
            "OpenCV cases were omitted.",
            "No AMD GPU.",
        ],
    )
    def test_qualified_summary_cannot_be_overridden_by_the_reason(self, tmp_path: Path, summary: str) -> None:
        """The exact reason cannot erase software scope recorded in the observation's summary."""
        path = tmp_path / ".notes" / "known-limits.json"
        reason = "This host has no NVIDIA GPU."
        record = _cuda_record(reason=reason, summary=summary, deduction=0.03)
        entry = {**_CUDA_LIMIT, "reason": reason}
        limit = gap_ledger.KnownLimit(**_known(entry))
        assert gap_ledger.apply_known_limit(record, [limit]) == record
        assert gap_ledger.register_known_limits(path, "octo/demo", [record]) == ([], None)
        assert not path.exists()
        assert gap_ledger._limit_errors(entry) == []  # A registry entry contains only the reason.

    def test_stale_lock_of_a_dead_writer_is_recovered(self, tmp_path: Path) -> None:
        """A lock directory older than the stale bound is removed once, so registration still happens."""
        path = tmp_path / ".notes" / "known-limits.json"
        lock = path.with_name(f".{path.name}.lock")
        lock.mkdir(parents=True)
        old = lock.stat().st_mtime - gap_ledger.STALE_LOCK_SECONDS - 60
        os.utime(lock, (old, old))
        assert gap_ledger.register_known_limits(path, "octo/demo", [_cuda_record(deduction=0.03)]) == (
            ["cuda-absent"],
            None,
        )
        assert not lock.exists()

    def test_live_lock_skips_registration(self, tmp_path: Path) -> None:
        """A fresh lock belongs to a live writer: this registration is skipped and the lock is left alone."""
        path = tmp_path / ".notes" / "known-limits.json"
        lock = path.with_name(f".{path.name}.lock")
        lock.mkdir(parents=True)
        added, error = gap_ledger.register_known_limits(path, "octo/demo", [_cuda_record(deduction=0.03)])
        assert (added, lock.is_dir(), path.exists()) == ([], True, False)
        assert error is not None


class TestCanonicalIds:
    """The executable ID table and the contract's published table name the same IDs and classes."""

    def test_contract_table_matches_the_executable_table(self) -> None:
        """Every row of the contract's Canonical gap IDs table equals one ``GAP_IDS`` entry, and none is missing."""
        contract = Path(gap_ledger.__file__).resolve().parents[1] / "skills" / "_shared" / "gap-ledger.md"
        lines = contract.read_text(encoding="utf-8").splitlines()
        start = lines.index("## Canonical gap IDs")
        rows = {}
        for line in lines[start + 1 :]:
            if line.startswith("## "):
                break
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if line.startswith("| `") and len(cells) >= 3:
                rows[cells[0]] = (cells[1], cells[2])
        assert rows == gap_ledger.GAP_IDS

    def test_unrun_implementation_challenge_is_a_deducted_process_gap(self) -> None:
        """An implementation challenge that did not run is a workflow process gap that keeps its deduction."""
        assert gap_ledger.lookup_gap_id("implementation-challenge-not-run") == ("process", "workflow")
        gap = _record(
            id="implementation-challenge-not-run",
            **{"class": "process", "owner": "workflow"},
            summary="The user declined the bounded implementation challenge.",
            deduction=0.04,
        )
        assert gap_ledger.record_errors(gap) == []
        assert gap_ledger.record_errors({**gap, "class": "human-decision", "owner": "user"}) == [
            "implementation-challenge-not-run requires class process and owner workflow"
        ]
        assert "known_limit applies to the hardware-absent class only" in gap_ledger.record_errors(
            {**gap, "known_limit": True, "deduction": None}
        )


@pytest.mark.parametrize(
    ("observation", "hardware"),
    [
        pytest.param("Multi-GPU tests skipped: only 1 GPU on this host.", "multi-gpu", id="digit-capacity"),
        pytest.param("Just one GPU on this host.", "multi-gpu", id="qualified-capacity"),
        pytest.param("One GPU on this host.", "multi-gpu", id="unqualified-capacity"),
        pytest.param("No second GPU available.", "multi-gpu", id="ordinal-capacity"),
        pytest.param("No GPU on this host.", "multi-gpu", id="generic-zero-capacity"),
        pytest.param("The runner has 0 GPUs.", "multi-gpu", id="numeric-zero-capacity"),
        pytest.param("Zero NVIDIA GPUs on this host.", "cuda", id="class-zero-capacity"),
        pytest.param("Only one GPU passed the benchmark.", None, id="test-result"),
        pytest.param("1 of 8 NVIDIA GPUs was missing from the node.", None, id="partitive-subset"),
        pytest.param("Two NVIDIA GPUs were absent from the pool.", None, id="numeric-subset"),
        pytest.param("One of our NVIDIA GPUs is missing.", None, id="partitive-owner"),
        pytest.param("Not all NVIDIA GPUs were missing.", None, id="negated-universal"),
        pytest.param("Two discrete NVIDIA GPUs were absent from the pool.", None, id="qualified-subset"),
        pytest.param("The first NVIDIA GPU is missing.", None, id="ordinal-subset"),
        pytest.param("No NVIDIA GPUs were missing.", None, id="negated-absence"),
        pytest.param("cuda-absent: Two NVIDIA GPUs were absent.", None, id="label-with-subset"),
    ],
)
def test_device_inventory_classifies_capacity(observation: str, hardware: str | None) -> None:
    """Only a class-wide absence or a single-GPU capacity constraint establishes a hardware class."""
    assert gap_ledger.absent_hardware_class(observation) == hardware


@pytest.mark.parametrize(
    ("observation", "hardware"),
    [
        pytest.param("No CUDA device available because torch is a CPU-only build.", None, id="prefix-cause"),
        pytest.param("NVIDIA GPU unavailable due to a CPU-only torch build.", None, id="postfix-build"),
        pytest.param("No NVIDIA GPU available because the driver is not installed.", None, id="prefix-driver"),
        pytest.param("NVIDIA GPU unavailable because the CUDA toolkit is missing.", None, id="postfix-toolkit"),
        pytest.param("CUDA device unavailable after installing the wrong NVIDIA driver.", None, id="setup-sequence"),
        pytest.param("CUDA tests skipped: NVIDIA GPU missing because the driver is not installed.", None, id="missing"),
        pytest.param("cuda-absent: The CUDA dependency is missing.", None, id="label-with-dependency"),
        pytest.param("NVIDIA GPU unavailable (the driver is not installed).", None, id="parenthetical-cause"),
        pytest.param("NVIDIA GPU missing — the driver is not installed.", None, id="dash-cause"),
        pytest.param("NVIDIA driver is missing.", None, id="software-only"),
        pytest.param("The driver is not installed, so the host has no NVIDIA GPU available.", None, id="leading-cause"),
        pytest.param("No NVIDIA GPU; the CUDA driver is not installed.", "cuda", id="independent-absence"),
    ],
)
def test_hardware_record_requires_absence_independent_of_provisioning(observation: str, hardware: str | None) -> None:
    """Classification and staged-record validation use the same device and cause evidence."""
    assert gap_ledger.absent_hardware_class(observation) == hardware
    assert bool(gap_ledger.record_errors(_cuda_record(reason=observation))) is (hardware is None)
    if hardware is None:
        assert gap_ledger.provisioning_cause(observation)


@pytest.mark.parametrize(
    "observation",
    [
        "known limit: This host has no NVIDIA GPU.",
        "not reproducible on this host: requires CUDA; host has no CUDA device (inventory probe).",
        "cuda-absent: Test runner: no NVIDIA GPU (known limit).",
    ],
)
def test_documented_hardware_observation_keeps_waiver(observation: str) -> None:
    """Report presentation can wrap a physical observation without granting a software-only waiver."""
    record = _cuda_record(reason=observation, summary=observation, deduction=0.03)
    applied = gap_ledger.apply_known_limit(record, [gap_ledger.KnownLimit(**_known(_CUDA_LIMIT))])
    assert (applied.get("known_limit"), applied["deduction"]) == (True, None)


def test_summary_provisioning_cause_refuses_hardware_record() -> None:
    """A physical reason cannot replace the summary's explanation for the same gap."""
    record = _cuda_record(summary="CUDA tests skipped because the CUDA toolkit is missing.")
    assert "summary names a software provisioning cause for the gap" in gap_ledger.record_errors(record)


def test_report_wrapper_preserves_software_qualifier() -> None:
    """A scoped software check does not become a hardware-inventory citation inside a report wrapper."""
    observation = "not reproducible on this host: requires CUDA; no NVIDIA GPU (driver scope check)."
    record = _cuda_record(reason=observation, summary=observation, deduction=0.03)
    assert gap_ledger.apply_known_limit(record, [gap_ledger.KnownLimit(**_known(_CUDA_LIMIT))]) == record


def test_removed_registry_entry_allows_closing_historical_waiver(tmp_path: Path) -> None:
    """A removed limit does not block a closing event for a previously accepted waiver."""
    ledger = tmp_path / "gaps.jsonl"
    stage = ledger.with_suffix(".jsonl.rec")
    limit = gap_ledger.KnownLimit(**_known(_CUDA_LIMIT))
    record = gap_ledger.apply_known_limit(_cuda_record(), [limit])
    _write_lines(stage, [record])
    assert gap_ledger.append_staged(ledger, _limits_file(tmp_path, [_CUDA_LIMIT])) == (1, [])
    closing = {key: value for key, value in record.items() if key != "known_limit"}
    _write_lines(stage, [{**closing, "status": "closed"}])
    assert gap_ledger.append_staged(ledger, _limits_file(tmp_path, [])) == (1, [])
    assert gap_ledger.latest_by_id(gap_ledger.load_ledger(ledger).records)["cuda-absent"]["status"] == "closed"


def test_schema_command_lists_the_closed_sets(capsys: pytest.CaptureFixture[str]) -> None:
    """``schema`` prints the class and route sets emitters must use."""
    assert gap_ledger.main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["class"] == [
        "process",
        "environment-provisionable",
        "hardware-absent",
        "project",
        "contribution",
        "human-decision",
    ]
    assert schema["run.route"] == ["direct", "paged", "serial-fallback", "parent-substitute", "none"]
