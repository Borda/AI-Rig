"""Exercise versioned review publication and resolver uncertainty through real artifacts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest.mock as mock
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import append_resolution as ar
import find_review_report as frr
import merge_action_items as mai
import pytest
from _review_fixtures import assessment, concern, finding_row, without
from _text_io import flat, write_lf

_BASH = shutil.which("bash")
_skip_bash_unavailable = pytest.mark.skipif(_BASH is None, reason="bash unavailable")

_PLUGIN = Path(__file__).resolve().parents[2]
_SIDECAR = "review-validation.json"
_HEAD = "abcdef1234567"
_ONLINE_ITEM = {"id": 1, "type": "[gh][req]", "summary": "independent online obligation"}

Mutation = Callable[[dict, list], None]


# --- file helpers ------------------------------------------------------------------------------------------------


def _seed(directory: Path, files: dict[str, str]) -> None:
    """Write each ``name: text`` pair of ``files`` into ``directory``."""
    for name, text in files.items():
        write_lf(directory / name, text)


def _write_rows(path: Path, rows: list[dict]) -> None:
    """Write ``rows`` as JSON lines."""
    write_lf(path, "".join(json.dumps(row) + "\n" for row in rows))


def _read_rows(path: Path) -> list[dict]:
    """Read a JSON-lines file back into dicts."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _read_envelope(run_dir: Path) -> dict:
    """Load the validation sidecar published beside a report."""
    return json.loads((run_dir / _SIDECAR).read_text(encoding="utf-8"))


def _save_envelope(run_dir: Path, data: dict) -> None:
    """Overwrite the validation sidecar with ``data``."""
    write_lf(run_dir / _SIDECAR, json.dumps(data))


def _edit_envelope(run_dir: Path, mutate: Callable[[dict], object]) -> None:
    """Load the sidecar, apply ``mutate`` to it, and save it back."""
    data = _read_envelope(run_dir)
    mutate(data)
    _save_envelope(run_dir, data)


def _write_envelope(run_dir: Path, data: dict, findings: list[dict]) -> None:
    """Persist a mutated envelope and its actionable rows beside the report."""
    _save_envelope(run_dir, data)
    _write_rows(run_dir / "findings.jsonl", findings)


def _write_head(run_dir: Path, sha: str = _HEAD) -> None:
    """Record the reviewed head commit beside the report."""
    write_lf(run_dir / "head-sha.txt", f"{sha}\n")


def _write_legacy_report(
    path: Path, gate: str = "BLOCK", outcome: str = "REQUEST_CHANGES", title: str = "Old", summary: str = "old"
) -> None:
    """Write a pre-contract report header: no ``Review contract``, ``Rating scale`` or ``Readiness`` marker."""
    write_lf(path, f"---\nTitle: {title}\nPR: #7\nGate: {gate}\nOutcome: {outcome}\nSummary: {summary}\n---\n")


def _bash_blocks(text: str) -> list[str]:
    """Return the body of every fenced ``bash`` block in Markdown ``text``, wherever the fence is indented."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if current is None:
            if stripped.startswith("```bash"):
                current = []
        elif stripped.startswith("```"):
            blocks.append("".join(current))
            current = None
        else:
            current.append(line)
    return blocks


# --- publication builders ----------------------------------------------------------------------------------------


def _publication(tmp_path: Path, outcome: str | None = "APPROVE", readiness: str = "complete") -> Path:
    """Write a complete empty version-one review with an explicitly returned scoped assessment."""
    report = tmp_path / "review-report.md"
    gate = "REVIEW_V1_REJECT_SCOPE @abcdef1234567" if outcome == "REJECT" else "REVIEW_V1_PASS"
    write_lf(
        report,
        f"---\nTitle: Review\nPR: #7\nReview contract: 1\nRating scale: source-5-v1\n"
        f"Readiness: {readiness}\nGate: {gate}\nOutcome: {outcome or 'null'}\nSummary: Scoped judgment.\n---\n",
    )
    rating = {"APPROVE": 1, "MINOR_EDITS": 2, "NEEDS_WORK": 3, "INSUFFICIENT_BUT_PROMISING": 4, "REJECT": 5}.get(
        outcome
    )
    envelope = {
        "contract_version": 1,
        "rating_scale": "source-5-v1",
        "source_outcome": outcome,
        "source_rating": rating,
        "readiness": readiness,
        "readiness_reason": "backend unavailable" if readiness == "blocked" else "checked",
        "assessments": [
            {
                "role": "QA",
                "presence": "completed",
                "source_outcome": outcome,
                "source_rating": rating,
                "readiness": readiness,
                "coverage": "src/backend.py",
                "rationale": "inspected exact source",
                "evidence": ["src/backend.py:12"],
                "limits": [],
            }
        ],
        "candidates": [],
        "open_concerns": [],
    }
    _save_envelope(tmp_path, envelope)
    write_lf(tmp_path / "findings.jsonl", "")
    return report


def _candidate(disposition: str | None) -> dict:
    """Describe a source-bound backend claim without pretending simulation covers real hardware."""
    return {
        "id": "backend-one",
        "claim": "backend drops the last value",
        "revision": _HEAD,
        "contract": "all input values preserved",
        "trigger": "two values on native backend",
        "counterevidence": ["generic CI does not assert native outputs"],
        "limits": ["native backend unavailable"],
        "checks": [
            {
                "method": "source inspection and faithful CPU simulation",
                "result": "represented control flow preserves values",
                "revision": _HEAD,
                "environment": "CPU; native backend not represented",
                "limits": ["native allocation"],
            }
        ],
        "disposition": disposition,
        "reason": "hardware-specific assertion still required",
        "feasible_checks_complete": disposition is not None,
        "missing_capability": "native backend hardware",
    }


def _named_candidate(identifier: str, disposition: str | None) -> dict:
    """Clone the evidence-bound candidate under another id."""
    return {**_candidate(disposition), "id": identifier}


@pytest.fixture(name="blocked_unverified")
def _blocked_unverified(tmp_path: Path) -> Path:
    """Publish a blocked review whose one UNVERIFIED backend candidate stays open with its closure handoff."""
    report = _publication(tmp_path, None, "blocked")
    concern = {
        "candidate_id": "backend-one",
        "owner": "backend maintainer",
        "next_check": "run exact native assertion",
        "closure": "all values preserved on reviewed head",
    }
    _edit_envelope(tmp_path, lambda data: data.update(candidates=[_candidate("UNVERIFIED")], open_concerns=[concern]))
    return report


@pytest.fixture(name="confirmed_publication")
def _confirmed_publication(tmp_path: Path) -> Path:
    """Publish a strictly valid version-one review whose one confirmed candidate has a merge-ready row."""
    report = _publication(tmp_path, "NEEDS_WORK")
    data = _read_envelope(tmp_path)
    data["candidates"] = [_candidate("CONFIRMED")]
    _write_envelope(tmp_path, data, [finding_row("backend-one")])
    return report


@pytest.fixture(name="pr8_like")
def _pr8_like(tmp_path: Path) -> Path:
    """Reproduce the rejected PR 8 publication shape under a claimed-complete header.

    Three UNVERIFIED concerns stay open, four reviewers are blocked and one is incomplete, and one CONFIRMED candidate
    (V1b) never got its findings.jsonl row while another (V2) did.
    """
    report = _publication(tmp_path, "NEEDS_WORK")
    data = _read_envelope(tmp_path)
    data["assessments"] = [
        assessment("Software engineer", "blocked"),
        assessment("QA specialist", "blocked"),
        assessment("Performance engineer", "blocked"),
        assessment("Challenger", "blocked"),
        assessment("Codex", "incomplete"),
    ]
    open_ids = ["V13", "V20", "V21"]
    data["candidates"] = [
        *(_named_candidate(cid, "UNVERIFIED") for cid in open_ids),
        _named_candidate("V1b", "CONFIRMED"),
        _named_candidate("V2", "CONFIRMED"),
    ]
    data["open_concerns"] = [concern(cid) for cid in open_ids]
    _write_envelope(tmp_path, data, [finding_row("V2")])
    return report


# --- mutations of a published envelope (also used as parametrized damage) ----------------------------------------


def _no_mutation(_data: dict) -> None:
    """Leave the envelope as published."""


def _open_unverified_under_complete(data: dict) -> None:
    """Leave one UNVERIFIED candidate open while the aggregate still claims completion."""
    data["candidates"] = [_candidate("UNVERIFIED")]
    data["open_concerns"] = [
        {"candidate_id": "backend-one", "owner": "maintainer", "next_check": "native run", "closure": "assert"}
    ]


def _blocked_assessment_under_complete(data: dict) -> None:
    """Report one reviewer as blocked while the aggregate still claims completion."""
    data["assessments"][0]["readiness"] = "blocked"


def _confirmed_without_finding_row(data: dict) -> None:
    """Confirm a candidate without publishing its actionable findings.jsonl row."""
    data["candidates"] = [_candidate("CONFIRMED")]


def _recheck(items: Path, review: Path, head: str) -> int:
    """Run resolve's verdict re-check on ``items`` against the review in ``review`` at commit ``head``."""
    return mai.main(["--items", str(items), "--recheck-verdicts", "--review-dir", str(review), "--current-head", head])


def _no_damage(_report: Path) -> None:
    """Leave the publication as written."""


def _row_without(field: str) -> Callable[[Path], None]:
    """Return a damage that drops one merge-required field from the only findings row."""

    def damage(report: Path) -> None:
        """Rewrite findings.jsonl beside ``report`` without the field."""
        _write_rows(report.with_name("findings.jsonl"), [without(finding_row("backend-one"), field)])

    return damage


def _row_severity(severity: str) -> Callable[[Path], None]:
    """Return a damage that gives the only findings row another severity word."""

    def damage(report: Path) -> None:
        """Rewrite findings.jsonl beside ``report`` with the severity word."""
        _write_rows(report.with_name("findings.jsonl"), [{**finding_row("backend-one"), "severity": severity}])

    return damage


def _second_row_reusing_id(report: Path) -> None:
    """Confirm a second candidate whose findings row reuses the first row's id, which intake does not check."""
    data = _read_envelope(report.parent)
    data["candidates"].append(_named_candidate("second-one", "CONFIRMED"))
    rows = [finding_row("backend-one"), {**finding_row("second-one"), "id": "f-backend-one"}]
    _write_envelope(report.parent, data, rows)


def _legacy_shaped_header(report: Path) -> None:
    """Rewrite the header without any version-one marker, which intake reads as a legacy archive."""
    _write_legacy_report(report, outcome="NEEDS_WORK", title="Review", summary="s")


def _set(path: tuple, value: object) -> Mutation:
    """Return a mutation that overwrites one nested envelope field, the way the mutation fuzz damages a sidecar."""

    def mutate(data: dict, _rows: list) -> None:
        """Walk ``path`` through the envelope and overwrite its last key."""
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _blocked_int_role(data: dict, _rows: list) -> None:
    """Rename the only reviewer to a number while it reports blocked under a claimed-complete review."""
    data["assessments"][0].update(role=7, readiness="blocked")


def _missing_with_judgment(data: dict, _rows: list) -> None:
    """Record a reviewer whose output is missing yet who still carries a source judgment."""
    data["assessments"].append({**assessment("Performance engineer", "complete"), "presence": "missing"})


def _list_finding_candidate(_data: dict, rows: list) -> None:
    """Publish an extra actionable row whose candidate_id is a list."""
    rows.append({**finding_row("V9"), "candidate_id": ["V9"]})


def _open_concern_under_complete(data: dict, rows: list) -> None:
    """Leave one UNVERIFIED candidate open, with its concern, while the aggregate claims completion."""
    data["candidates"].append(_named_candidate("open-one", "UNVERIFIED"))
    data["open_concerns"].append(concern("open-one"))


def _label_slip_row_without_change(data: dict, rows: list) -> None:
    """Add a label slip and drop the ``change`` field resolve's merge needs from the only findings row."""
    _open_concern_under_complete(data, rows)
    del rows[0]["change"]


def _label_slip_row_unknown_severity(data: dict, rows: list) -> None:
    """Add a label slip and give the only findings row a severity outside the merge's vocabulary."""
    _open_concern_under_complete(data, rows)
    rows[0]["severity"] = "major"


def _label_slip_rows_sharing_an_id(data: dict, rows: list) -> None:
    """Add a label slip and a second confirmed candidate whose findings row reuses the first row's id."""
    _open_concern_under_complete(data, rows)
    data["candidates"].append(_named_candidate("second-one", "CONFIRMED"))
    rows.append({**finding_row("second-one"), "id": rows[0]["id"]})


def _label_slip_unattributed_row_without_change(data: dict, rows: list) -> None:
    """Add a label slip and a row lacking ``change`` whose candidate_id names no recorded candidate."""
    _open_concern_under_complete(data, rows)
    rows.append(without(finding_row("ghost"), "change"))


def _judged_reviewer(data: dict) -> None:
    """Let the only reviewer judge NEEDS_WORK while the aggregate outcome stays null."""
    data["assessments"][0].update(source_outcome="NEEDS_WORK", source_rating=3)


def _missing_reviewer(data: dict) -> None:
    """Record the only reviewer's output as missing, so no reviewer supports any aggregate outcome."""
    data["assessments"][0].update(presence="missing", readiness="incomplete")


def _nest(depth: int) -> object:
    """Return a string wrapped in ``depth`` single-item lists."""
    value: object = "deep"
    for _ in range(depth):
        value = [value]
    return value


#: A completed reviewer whose output never arrived, as the protocol records it.
_MISSING_REVIEWER: dict = {
    "role": "Performance engineer",
    "presence": "missing",
    "source_outcome": None,
    "source_rating": None,
    "readiness": "incomplete",
    "coverage": "owed: hot path",
    "rationale": "",
    "evidence": [],
    "limits": ["no output"],
}


class TestReportCli:
    """The report reader's console encoding."""

    @staticmethod
    def _run(report_dir: Path, *extra: str) -> subprocess.CompletedProcess[bytes]:
        """Run the real CLI on a blocked publication under a cp1252 console."""
        report_dir.mkdir(exist_ok=True)
        report = _publication(report_dir, None, "blocked")
        args = [sys.executable, str(Path(frr.__file__)), "--report", str(report), *extra]
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        return subprocess.run(args, env=env, capture_output=True, check=False, timeout=15)

    def test_stdout_is_utf8_with_a_non_utf8_console(self, tmp_path: Path) -> None:
        """Gate verdicts are decodable as UTF-8 on stdout regardless of the console encoding.

        The report lives under a directory with an accented name and the verdict text contains an em dash; a console
        limited to cp1252 must not change the bytes shell consumers decode, nor turn newlines into CRLF.
        """
        result = self._run(tmp_path / "review-é")
        output = result.stdout.decode("utf-8")
        assert result.returncode == 0, result.stderr
        assert "é" in output
        assert "—" in output
        assert "\r\n" not in output

    def test_stderr_is_utf8_with_a_non_utf8_console(self, tmp_path: Path) -> None:
        """Path diagnostics are decodable as UTF-8 on stderr regardless of the console encoding.

        Asking for ``--path-out`` under a directory that does not exist makes the CLI fail and name the accented path on
        stderr; those bytes must still be UTF-8 with LF newlines.
        """
        report_dir = tmp_path / "review-é"
        result = self._run(report_dir, "--path-out", str(report_dir / "absent" / "selected.txt"))
        output = result.stderr.decode("utf-8")
        assert result.returncode == 1, result.stderr
        assert "é" in output
        assert "—" in output
        assert "\r\n" not in output


class TestPublicationIntake:
    """Resolve's strict intake of a version-one publication."""

    @pytest.mark.parametrize(
        ("outcome", "readiness", "expected"),
        [
            pytest.param("MINOR_EDITS", "complete", "Gate: PASS", id="minor-edits"),
            pytest.param(
                "INSUFFICIENT_BUT_PROMISING",
                "complete",
                "Gate: PASS",
                id="good-initiative-substantially-incomplete-source",
            ),
            pytest.param("REJECT", "complete", "Gate: REJECT_SCOPE @abcdef1234567", id="new-five-is-block"),
            pytest.param(None, "blocked", "Gate: BLOCK", id="completed-undecidable-not-rejected"),
        ],
    )
    def test_separates_source_outcome_from_readiness(
        self, tmp_path: Path, outcome: str | None, readiness: str, expected: str
    ) -> None:
        """The normal report reader accepts new meanings only with their versioned assessment evidence.

        A source outcome (including the new rejection) and a readiness are published separately; a completed but
        undecidable review is a block, never a rejection.
        """
        report = _publication(tmp_path, outcome, readiness)
        assert frr.gate_line(report) == expected

    def test_missing_reviewer_is_not_completed_null(self, tmp_path: Path) -> None:
        """A missing response cannot satisfy a completed-but-undecidable assessment.

        The only reviewer is marked missing while the aggregate still says null and blocked; that must not read as a
        valid gate.
        """
        report = _publication(tmp_path, None, "blocked")
        _edit_envelope(tmp_path, lambda data: data["assessments"][0].update(presence="missing"))
        assert frr.gate_line(report) == ""

    def test_unknown_contract_version_exits_1(self, tmp_path: Path) -> None:
        """An unsupported contract version fails closed instead of entering resolve."""
        report = _publication(tmp_path)
        _edit_envelope(tmp_path, lambda data: data.update(contract_version=2))
        assert frr.main(["--report", str(report)]) == 1

    def test_header_hiding_a_different_judgment_is_rejected(self, tmp_path: Path) -> None:
        """A favorable header over a sidecar recording another source outcome yields no gate line.

        The header says APPROVE while the sidecar records NEEDS_WORK; trusting the header would let a hidden judgment
        through.
        """
        report = _publication(tmp_path)
        _edit_envelope(tmp_path, lambda data: data.update(source_outcome="NEEDS_WORK"))
        assert frr.gate_line(report) == ""

    @pytest.mark.parametrize("marker", ["Rating scale: source-5-v1", "Readiness: complete"])
    def test_partial_current_header_cannot_downgrade_to_legacy(self, tmp_path: Path, marker: str) -> None:
        """Any current-family marker requires the supported complete publication envelope.

        A header carrying only one of the version-one markers must not be read as a legacy archive that skips the
        sidecar rules.
        """
        report = _publication(tmp_path)
        write_lf(
            report,
            f"---\nTitle: Review\nPR: #7\n{marker}\nGate: PASS\nOutcome: APPROVE\nSummary: partial migration\n---\n",
        )
        assert frr.main(["--report", str(report)]) == 1

    def test_legacy_four_keeps_insufficient_evidence_meaning(self, tmp_path: Path) -> None:
        """Historical four and REQUEST_CHANGES remain readable without new rejection semantics.

        An archived report that rated a reviewer 4 (insufficient evidence) keeps its BLOCK gate and its legend.
        """
        report = tmp_path / "review-report.md"
        write_lf(
            report,
            "---\nTitle: Old\nPR: #7\nGate: BLOCK\nOutcome: REQUEST_CHANGES\nSummary: evidence incomplete\n"
            "Reviewers: QA (4)\n---\nLegend: 4 = Insufficient evidence · 5 = Block / Reject.\n",
        )
        assert frr.gate_line(report) == "Gate: BLOCK"
        assert "4 = Insufficient evidence" in report.read_text(encoding="utf-8")

    def test_open_concern_survives_intake_without_actionable_finding(self, blocked_unverified: Path) -> None:
        """Bound simulation can leave only backend uncertainty open; its closure handoff survives discovery.

        A blocked review with one UNVERIFIED candidate and its durable concern is selected as the report to resolve, and
        the concern's owner is still recorded in the sidecar.
        """
        run = blocked_unverified.parent
        path_out = run / "selected-report.txt"
        assert frr.main(["--report", str(blocked_unverified), "--path-out", str(path_out)]) == 0
        assert path_out.read_text(encoding="utf-8").strip() == blocked_unverified.as_posix()
        assert _read_envelope(run)["open_concerns"][0]["owner"] == "backend maintainer"

    def test_open_concern_needs_complete_feasible_checks(self, blocked_unverified: Path) -> None:
        """An UNVERIFIED candidate whose feasible checks are not all done is not an accepted open concern."""
        run = blocked_unverified.parent
        _edit_envelope(run, lambda data: data["candidates"][0].update(feasible_checks_complete=False))
        assert frr.gate_line(blocked_unverified) == ""

    @pytest.mark.parametrize(
        ("row_count", "expected"),
        [
            pytest.param(0, "", id="row-missing"),
            pytest.param(1, "Gate: PASS", id="one-row"),
            pytest.param(2, "", id="row-published-twice"),
        ],
    )
    def test_confirmed_candidate_has_exactly_one_row(self, tmp_path: Path, row_count: int, expected: str) -> None:
        """Confirmed obligations and actionable records have one-to-one correspondence.

        A confirmed candidate cannot disappear (no row) or publish twice (two rows); only exactly one row passes.
        """
        report = _publication(tmp_path, "NEEDS_WORK")
        _edit_envelope(tmp_path, lambda data: data.update(candidates=[_candidate("CONFIRMED")]))
        _write_rows(tmp_path / "findings.jsonl", [finding_row("backend-one")] * row_count)
        assert frr.gate_line(report) == expected

    @pytest.mark.parametrize(
        ("recorded_head", "check_revision", "expected"),
        [
            pytest.param("different-reviewed-head", _HEAD, "", id="recorded-head-differs"),
            pytest.param(_HEAD, _HEAD, "Gate: PASS", id="evidence-matches-head"),
            pytest.param(_HEAD, "another-source-head", "", id="check-revision-differs"),
        ],
    )
    def test_candidate_evidence_must_match_recorded_review_head(
        self, confirmed_publication: Path, recorded_head: str, check_revision: str, expected: str
    ) -> None:
        """A report's frozen head cannot silently reuse candidate/check evidence from another revision.

        The gate passes only when both the recorded head file and every check's revision equal the candidate's revision.
        """
        run = confirmed_publication.parent
        _write_head(run, recorded_head)
        _edit_envelope(run, lambda data: data["candidates"][0]["checks"][0].update(revision=check_revision))
        assert frr.gate_line(confirmed_publication) == expected

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            pytest.param(
                _open_unverified_under_complete,
                "open candidate 'backend-one' (UNVERIFIED) under readiness=complete",
                id="open-concern-under-complete",
            ),
            pytest.param(
                _blocked_assessment_under_complete,
                "assessment 'QA' readiness=blocked under aggregate readiness=complete",
                id="blocked-assessment-under-complete",
            ),
            pytest.param(
                _confirmed_without_finding_row,
                "CONFIRMED candidate(s) without findings.jsonl row: backend-one",
                id="confirmed-without-finding-row",
            ),
        ],
    )
    def test_rejection_names_the_broken_rule(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], mutate: Callable[[dict], None], reason: str
    ) -> None:
        """An inconsistent envelope is refused with the exact rule it breaks, not a bare incomplete verdict.

        Each case is one of the producer slips seen in a real rejected publication: open concerns, a blocked reviewer,
        or a confirmed candidate missing its finding row, all under ``Readiness: complete``. Without the reason, the
        producer's retry and the user both have to re-derive the cause by calling the private checks by hand.
        """
        report = _publication(tmp_path, "NEEDS_WORK")
        _edit_envelope(tmp_path, mutate)
        assert frr.main(["--report", str(report)]) == 1
        assert f"[gate] reason: {reason}" in capsys.readouterr().out

    def test_string_assessment_evidence_is_equivalent_to_a_one_item_list(self, tmp_path: Path) -> None:
        """A reviewer that wrote evidence and limits as plain strings still publishes a readable gate.

        The type differs, not the meaning, so the intake gate must not stop an otherwise valid envelope on it; the
        producer contract still asks for arrays.
        """
        report = _publication(tmp_path, "NEEDS_WORK")
        _edit_envelope(
            tmp_path, lambda data: data["assessments"][0].update(evidence="src/backend.py:12", limits="none")
        )
        assert frr.gate_line(report) == "Gate: PASS"

    @pytest.mark.parametrize(
        ("disposition", "rows"),
        [
            pytest.param("CONFIRMED", [finding_row("backend-one")], id="concern-for-confirmed-candidate"),
            pytest.param("UNVERIFIED", [], id="unverified-candidate-under-complete-readiness"),
        ],
    )
    def test_open_concern_beside_a_closed_or_complete_review_is_rejected(
        self, tmp_path: Path, disposition: str, rows: list[dict]
    ) -> None:
        """``open_concerns`` lists only open candidates, and an open UNVERIFIED one forbids readiness complete.

        A concern naming an already CONFIRMED candidate is invalid, and so is an UNVERIFIED candidate left open under an
        aggregate that still claims completion.
        """
        report = _publication(tmp_path, "NEEDS_WORK")
        data = _read_envelope(tmp_path)
        data["candidates"] = [_candidate(disposition)]
        data["open_concerns"] = [
            {"candidate_id": "backend-one", "owner": "maintainer", "next_check": "CI", "closure": "CI passes"}
        ]
        _write_envelope(tmp_path, data, rows)
        assert frr.gate_line(report) == ""


class TestFindingConsumers:
    """Resolve's consumers of validated findings, merged items and resolver verdicts."""

    def test_unknown_finding_disposition_cannot_become_action(self, tmp_path: Path) -> None:
        """The actionable finding consumer rejects uncertainty even when the record has all legacy fields.

        A row whose validation status is UNVERIFIED must stop the merge and create no action-items file.
        """
        finding = {**finding_row("backend-one"), "validation_status": "UNVERIFIED"}
        findings = tmp_path / "findings.jsonl"
        _write_rows(findings, [finding])
        items = tmp_path / "items.jsonl"
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 1
        assert not items.exists()

    @pytest.mark.parametrize(
        ("resolution", "evidence", "follow_up"),
        [
            pytest.param("unverified", "UNVERIFIED", "next_check=backend CI", id="unverified-keeps-follow-up"),
            pytest.param("already-fixed", "ALREADY_FIXED", "", id="already-fixed"),
        ],
    )
    def test_resolution_consumer_preserves_non_refutation(
        self, tmp_path: Path, resolution: str, evidence: str, follow_up: str
    ) -> None:
        """A resolver's blocked evidence or later fix stays distinct from rejected and creates no commit lookup.

        Both verdicts keep their own name, record no commit sha, and carry the resolver's reason; an unverified item
        also keeps its follow-up handoff for the maintainer.
        """
        _seed(
            tmp_path,
            {
                "action-items.jsonl": json.dumps({"id": 1, "finding_id": "finding-one"}) + "\n",
                "selected-items.txt": "1\n",
                "challenge-log.txt": (
                    f"id=1 resolution={resolution} evidence={evidence} suggestion=— finding=f "
                    "evidence_why=current evidence unavailable suggestion_why=— detail=owner=maintainer; "
                    "next_check=backend CI; closure=exact assertion passes\n"
                ),
            },
        )
        record = ar.build_records(tmp_path, "run", tmp_path)[0]
        assert record["verdict"] == resolution
        assert record["sha"] == ""
        assert "current evidence unavailable" in record["why"]
        assert follow_up in record.get("follow_up", "")

    @pytest.mark.parametrize(
        "online_items",
        [
            pytest.param({}, id="no-online-item"),
            pytest.param(
                {
                    "items.jsonl": json.dumps({"id": 1, "file": "src/backend.py", "line": 12, "type": "[gh][req]"})
                    + "\n"
                },
                id="matching-online-item",
            ),
        ],
    )
    def test_merge_carries_parent_candidate_proof(
        self, confirmed_publication: Path, online_items: dict[str, str]
    ) -> None:
        """The merged item derives its proof pointer from the validated publication, independent of online matches.

        Whether or not an online item already covers the finding, the item points at the sidecar, names its candidate,
        keeps the CONFIRMED status, and never invents a critical-verification verdict.
        """
        review = confirmed_publication.parent
        _write_head(review)
        _seed(review, online_items)
        findings = review / "findings.jsonl"
        _write_rows(findings, [{**finding_row("backend-one"), "file": "src/backend.py", "line": 12}])
        items = review / "items.jsonl"
        assert frr.gate_line(confirmed_publication) == "Gate: PASS"
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        (item,) = _read_rows(items)
        assert item["parent_validation_file"] == (review / _SIDECAR).resolve().as_posix()
        assert item["candidate_id"] == "backend-one"
        assert item["validation_status"] == "CONFIRMED"
        assert "verify_verdict" not in item

    @pytest.mark.parametrize(
        ("current_head", "expected_status"),
        [
            pytest.param(_HEAD, "CONFIRMED", id="head-unchanged-keeps-proof"),
            pytest.param("bbbbbbb1234567", None, id="head-moved-drops-shortcut"),
        ],
    )
    def test_recheck_follows_the_reviewed_head(
        self, confirmed_publication: Path, current_head: str, expected_status: str | None
    ) -> None:
        """The verdict re-check keeps proof reusable only at the reviewed head, and always keeps its pointer.

        After the normal merge, a re-check at the same head retains CONFIRMED; at a moved head the shortcut is gone but
        the sidecar pointer and candidate id stay so the proof can still be inspected.
        """
        review = confirmed_publication.parent
        _write_head(review)
        items = review / "items.jsonl"
        assert mai.main(["--items", str(items), "--findings", str(review / "findings.jsonl")]) == 0
        merged = _read_rows(items)[0]
        assert _recheck(items, review, current_head) == 0
        (rechecked,) = _read_rows(items)
        assert rechecked.get("validation_status") == expected_status
        assert rechecked["parent_validation_file"] == merged["parent_validation_file"]
        assert rechecked["candidate_id"] == "backend-one"

    def test_root_cause_merged_row_passes_the_gate_and_folds_into_one_item(self, tmp_path: Path) -> None:
        """A confirmed candidate merged into another keeps its own row, and resolve offers one item for the pair.

        The producer gate maps findings rows one-to-one onto CONFIRMED candidates, so the merged candidate must publish
        its row; the merge then folds that row into the survivor's item, which keeps the survivor's parent proof through
        the verdict re-check at the reviewed head.
        """
        review = tmp_path / "review"
        review.mkdir()
        report = _publication(review, "NEEDS_WORK")
        _edit_envelope(
            review,
            lambda data: data.update(
                candidates=[_candidate("CONFIRMED"), _named_candidate("backend-two", "CONFIRMED")]
            ),
        )
        _write_head(review)
        survivor = {
            **finding_row("backend-one"),
            "id": "finding-one",
            "section": "Test Coverage Gaps",
            "file": "src/backend.py",
            "line": 12,
        }
        merged = {
            **finding_row("backend-two"),
            "id": "finding-two",
            "merged_into": "backend-one",
            "severity": "high",
            "title": "backend leaks a slot",
            "full_text": "[high] the drop path never releases its slot (src/backend.py:30)",
            "file": "src/backend.py",
            "line": 30,
        }
        findings = review / "findings.jsonl"
        _write_rows(findings, [survivor, merged])
        items = tmp_path / "items.jsonl"

        assert frr.gate_line(report) == "Gate: PASS"
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        assert _recheck(items, review, _HEAD) == 0

        (item,) = mai.read_jsonl(items)
        assert (item["finding_id"], item["merged_finding_ids"]) == ("finding-one", ["finding-two"])
        assert (item["type"], item["severity"]) == ("[report][req]", 4)
        assert (item["candidate_id"], item["validation_status"]) == ("backend-one", "CONFIRMED")
        assert "[merged finding finding-two] [high] the drop path never releases its slot" in item["full_comment_text"]

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(lambda review, _items: (review / _SIDECAR).unlink(), id="missing-sidecar"),
            pytest.param(
                lambda review, _items: _edit_envelope(
                    review, lambda data: data["candidates"][0]["checks"][0].update(result="different evidence")
                ),
                id="tampered-check",
            ),
            pytest.param(
                lambda review, _items: _edit_envelope(review, lambda data: data.update(candidates=[])),
                id="missing-candidate",
            ),
            pytest.param(
                lambda _review, items: _write_rows(
                    items, [{**_read_rows(items)[0], "candidate_id": "different-candidate"}]
                ),
                id="wrong-item-candidate",
            ),
            pytest.param(
                lambda review, _items: _write_legacy_report(
                    review / "review-report.md", gate="PASS", outcome="APPROVE", title="Archive", summary="historical"
                ),
                id="legacy-header",
            ),
        ],
    )
    def test_changed_proof_disables_shortcut_but_retains_pointer(
        self, confirmed_publication: Path, damage: Callable[[Path, Path], object]
    ) -> None:
        """A changed proof cannot remain reusable merely because the source head still matches.

        After a normal merge, the sidecar disappears, a check's result changes, the candidate vanishes, the item points
        at another candidate, or the report degrades to a legacy header. The re-check then drops the validation shortcut
        and any critical-verification verdict but keeps the pointer to the parent proof.
        """
        review = confirmed_publication.parent
        _write_head(review)
        items = review / "impl" / "items.jsonl"
        assert mai.main(["--items", str(items), "--findings", str(review / "findings.jsonl")]) == 0
        original = _read_rows(items)[0]
        damage(review, items)
        assert _recheck(items, review, _HEAD) == 0
        (stale,) = _read_rows(items)
        assert "validation_status" not in stale
        assert stale["parent_validation_file"] == original["parent_validation_file"]
        assert "verify_verdict" not in stale

    @pytest.mark.parametrize(
        ("damage", "message"),
        [
            pytest.param(
                _row_without("id"), "! BLOCKED — finding 1 missing id — run mint_finding_ids.py", id="row-without-id"
            ),
            pytest.param(
                _second_row_reusing_id,
                "! BLOCKED — duplicate finding id f-backend-one on findings 1 and 2",
                id="rows-sharing-an-id",
            ),
        ],
    )
    def test_recheck_blocks_on_a_publication_row_the_merge_refuses(
        self,
        confirmed_publication: Path,
        capsys: pytest.CaptureFixture[str],
        damage: Callable[[Path], None],
        message: str,
    ) -> None:
        """The verdict re-check refuses a validated publication holding rows the merge could not use, writing nothing.

        The intake gate checks neither row ids nor their uniqueness, so such a publication counts as valid; the re-check
        keys parent proof by finding id, so a missing id used to stop with a ``KeyError`` traceback and a repeated id
        would map one item's proof through whichever row came last. It now blocks before writing anything.
        """
        review = confirmed_publication.parent
        damage(confirmed_publication)
        _write_head(review)
        items = review / "items.jsonl"
        item = {"id": 1, "type": "[report][req]", "finding_id": "f-backend-one", "validation_status": "CONFIRMED"}
        _write_rows(items, [item])
        before = items.read_bytes()
        assert _recheck(items, review, _HEAD) == 1
        assert capsys.readouterr().err.startswith(message)
        assert items.read_bytes() == before


class TestReviewShellGates:
    """The shipped shell blocks that run these gates, executed against the real validator."""

    @staticmethod
    def _review_publication_gate() -> str:
        """Return review Step 5's publication-gate recipe, which must run before the Step 5b header print."""
        skill = (_PLUGIN / "skills/review/SKILL.md").read_text(encoding="utf-8")
        step5 = skill.split("## Step 5: Consolidate findings", 1)[1]
        step5 = step5.split('TaskUpdate "Step 5b: Print report header" → `in_progress`', 1)[0]
        return next(block for block in _bash_blocks(step5) if '--report "$_REPORT_DIR/review-report.md"' in block)

    @classmethod
    def _run_review_publication_gate(cls, report_dir: Path) -> subprocess.CompletedProcess[str]:
        """Run the shipped Step 5 block on the report in ``report_dir``, which is also the TMPDIR."""
        session = "publication-gate"
        write_lf(report_dir / f"oss-review-report-dir-{session}", report_dir.as_posix() + "\n")
        env = {
            **os.environ,
            "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
            "CLAUDE_CODE_SESSION_ID": session,
            "TMPDIR": report_dir.as_posix(),
        }
        return subprocess.run(
            [_BASH, "-c", cls._review_publication_gate()],
            env=env,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=15,
        )

    @_skip_bash_unavailable
    @pytest.mark.parametrize(
        ("mutate", "returncode", "expected"),
        [
            pytest.param(_no_mutation, 0, "[gate] Gate: PASS", id="consistent-publication-ships"),
            pytest.param(
                _open_unverified_under_complete, 1, "[gate] reason: open candidate", id="resolve-rejectable-stops"
            ),
        ],
    )
    def test_review_gates_its_own_publication(
        self, tmp_path: Path, mutate: Callable[[dict], None], returncode: int, expected: str
    ) -> None:
        """Oss:review runs resolve's intake check on the consolidator's output before printing it as finished.

        Regression: the producer published ``Readiness: complete`` with open UNVERIFIED concerns, and only
        ``/oss:resolve`` noticed. The recipe here is the shipped Step 5 block, run against the real validator.
        """
        report = _publication(tmp_path, "NEEDS_WORK")
        _edit_envelope(tmp_path, mutate)
        result = self._run_review_publication_gate(tmp_path)
        assert result.returncode == returncode, result.stdout + result.stderr
        assert expected in result.stdout
        assert report.exists()

    @_skip_bash_unavailable
    def test_review_publication_gate_refuses_a_row_resolve_cannot_merge(self, confirmed_publication: Path) -> None:
        """The shipped Step 5 gate runs the producer checks, so a row lacking ``author`` never ships as finished."""
        _write_rows(confirmed_publication.with_name("findings.jsonl"), [without(finding_row("backend-one"), "author")])
        result = self._run_review_publication_gate(confirmed_publication.parent)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "[gate] reason: findings.jsonl finding 1 missing author" in result.stdout

    @staticmethod
    def _stage_resolve_consumer(run_dir: Path, source_rel: str, seed: dict[str, str]) -> _ResolveConsumerRun:
        """Stage sentinels for the resolve block that persists findings, with ``seed`` files in its impl dir."""
        session = "empty-findings"
        impl = run_dir / "impl"
        impl.mkdir()
        _seed(impl, seed)
        write_lf(run_dir / f"resolve-impl-dir-{session}", impl.as_posix() + "\n")
        write_lf(run_dir / f"resolve-report-file-{session}", (run_dir / "review-report.md").as_posix() + "\n")
        source = (_PLUGIN / source_rel).read_text(encoding="utf-8")
        block = next(block for block in _bash_blocks(source) if "--persist-findings" in block)
        env = {
            **os.environ,
            "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
            "CLAUDE_CODE_SESSION_ID": session,
            "TMPDIR": run_dir.as_posix(),
        }
        return _ResolveConsumerRun(impl, block, env)

    @_skip_bash_unavailable
    @pytest.mark.parametrize(
        ("source_rel", "seed", "expected_items"),
        [
            pytest.param(
                "skills/resolve/SKILL.md",
                {"action-items.jsonl": json.dumps(_ONLINE_ITEM) + "\n"},
                [_ONLINE_ITEM],
                id="pr-mode-keeps-online-items",
            ),
            pytest.param("skills/resolve/modes/report-intelligence.md", {}, [], id="report-mode-creates-empty-items"),
        ],
    )
    def test_resolve_shell_consumers_accept_empty_confirmed_sidecar(
        self, blocked_unverified: Path, source_rel: str, seed: dict[str, str], expected_items: list[dict]
    ) -> None:
        """Validated uncertainty reaches report/PR handoff without legacy reparsing or dropping online items.

        The review has no confirmed findings, only an open concern. The shipped resolve block must still persist the
        findings sentinel, leave the sidecar and its concern byte-identical, and keep any online action items.
        """
        run = blocked_unverified.parent
        assert frr.main(["--report", str(blocked_unverified)]) == 0
        before = (run / _SIDECAR).read_bytes()
        concerns = _read_envelope(run)["open_concerns"]
        staged = self._stage_resolve_consumer(run, source_rel, seed)
        result = staged.run()
        assert result.returncode == 0, result.stdout + result.stderr
        assert (run / "resolve-findings-file-empty-findings").read_text(encoding="utf-8").strip() == (
            run / "findings.jsonl"
        ).as_posix()
        assert not (staged.impl / "report-findings.jsonl").exists()
        assert _read_rows(staged.impl / "action-items.jsonl") == expected_items
        assert (run / _SIDECAR).read_bytes() == before
        assert _read_envelope(run)["open_concerns"] == concerns

    @_skip_bash_unavailable
    @pytest.mark.parametrize(
        ("source_rel", "seed"),
        [
            pytest.param(
                "skills/resolve/SKILL.md", {"action-items.jsonl": json.dumps(_ONLINE_ITEM) + "\n"}, id="pr-mode"
            ),
            pytest.param("skills/resolve/modes/report-intelligence.md", {}, id="report-mode"),
        ],
    )
    def test_resolve_shell_consumers_fail_without_findings_sidecar(
        self, blocked_unverified: Path, source_rel: str, seed: dict[str, str]
    ) -> None:
        """Losing findings.jsonl remains a boundary failure, not permission to invent an empty collection."""
        run = blocked_unverified.parent
        (run / "findings.jsonl").unlink()
        result = self._stage_resolve_consumer(run, source_rel, seed).run()
        assert result.returncode != 0
        assert "no findings.jsonl" in result.stdout


@dataclass
class _ResolveConsumerRun:
    """A staged run of the shipped resolve shell block that persists a report's findings."""

    impl: Path
    block: str
    env: dict[str, str]

    def run(self) -> subprocess.CompletedProcess[str]:
        """Execute the block in bash with the staged environment."""
        return subprocess.run(
            [_BASH, "-c", self.block], env=self.env, encoding="utf-8", capture_output=True, check=False, timeout=15
        )


@pytest.fixture(name="prose", scope="module")
def _prose() -> dict[str, str]:
    """Return whitespace-flattened review skill, consolidator template, and resolve dispatch texts (read once)."""
    dispatch = "\n".join(
        (_PLUGIN / f"skills/resolve/modes/dispatch-{phase}.md").read_text(encoding="utf-8")
        for phase in ("prelude", "phase1", "phase2", "phase3")
    )
    return {
        "skill": flat((_PLUGIN / "skills/review/SKILL.md").read_text(encoding="utf-8")),
        "template": flat((_PLUGIN / "skills/review/templates/consolidator-prompt.md").read_text(encoding="utf-8")),
        "dispatch": flat(dispatch),
    }


class TestSkillProse:
    """Wording that review and resolve must keep in agreement."""

    @pytest.mark.parametrize(
        ("source", "statement"),
        [
            pytest.param("template", "terminal-ground=<GROUND> | evidence=<path>", id="template-route"),
            pytest.param("skill", "terminal-ground=<GROUND> | evidence=<path>", id="skill-route"),
            pytest.param("template", "publish nothing", id="template-publishes-nothing"),
            pytest.param(
                "skill",
                "Stage 1's existing confirmed-ground block and terminal publisher",
                id="skill-existing-publisher",
            ),
            pytest.param(
                "skill",
                "Retain the original reviewed SHA, exact ground/reason and provenance",
                id="skill-retains-provenance",
            ),
            pytest.param(
                "skill",
                "including its conduct/license confirmation safeguards",
                id="skill-keeps-confirmation-safeguards",
            ),
            pytest.param(
                "dispatch", "reuse applicable proved checks without rerunning them", id="dispatch-reuses-checks"
            ),
            pytest.param(
                "dispatch",
                "This parent proof never supplies the independent [confirmed] verifier marker",
                id="dispatch-parent-proof-not-verifier",
            ),
            pytest.param("dispatch", "Part 2 — for EVERY item whose problem exists", id="dispatch-part-2"),
        ],
    )
    def test_late_rejection_routes_to_existing_ground_publisher(
        self, prose: dict[str, str], source: str, statement: str
    ) -> None:
        """The two normal consumers agree that late rejection cannot publish with literal PASS/BLOCK.

        Text is compared with whitespace flattened so reflowing a paragraph cannot break the contract.
        """
        assert statement in prose[source]


class TestAssess:
    """``--assess`` grading of a publication that the strict gate refuses."""

    def test_grades_every_pr8_gap(self, pr8_like: Path) -> None:
        """All gaps are reported at once, the lost obligation and blocked QA as critical, the rest as degraded.

        The strict gate stops at the first broken rule; triage needs the whole picture to offer a rerun scope covering
        exactly the blocked reviewers and open candidates.
        """
        gaps = {(gap.rule, gap.severity.value, gap.subject) for gap in frr.assess_report(pr8_like).gaps}
        assert gaps == {
            ("assessment-not-complete", "degraded", "Software engineer"),
            ("assessment-not-complete", "critical", "QA specialist"),
            ("assessment-not-complete", "degraded", "Performance engineer"),
            ("assessment-not-complete", "degraded", "Challenger"),
            ("assessment-not-complete", "degraded", "Codex"),
            ("readiness-mislabel", "degraded", "V13"),
            ("readiness-mislabel", "degraded", "V20"),
            ("readiness-mislabel", "degraded", "V21"),
            ("finding-missing", "critical", "V1b"),
        }

    def test_degraded_only_report_stays_consumable_under_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A wrong readiness label alone leaves the confirmed findings usable, under a BLOCK gate.

        The open concern is intact and the QA reviewer finished, so nothing actionable is lost; resolve may offer to
        proceed with the valid parts. The path is published so that choice needs no second lookup.
        """
        report = _publication(tmp_path, "NEEDS_WORK")
        data = _read_envelope(tmp_path)
        data["candidates"] = [_candidate("UNVERIFIED")]
        data["open_concerns"] = [concern("backend-one")]
        _write_envelope(tmp_path, data, [])
        path_out = tmp_path / "selected.txt"
        assert frr.main(["--report", str(report), "--assess", "--path-out", str(path_out)]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert (payload["gate"], payload["critical"], payload["degraded"]) == ("Gate: BLOCK", 0, 1)
        assert path_out.read_text(encoding="utf-8").strip() == report.as_posix()

    def test_critical_report_is_not_consumable(self, pr8_like: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A lost obligation or blocked security review forbids consuming any part, and publishes no path.

        Exit 2 is distinct from 1 so the resolve recipe can tell "assessed, not consumable" from a broken lookup.
        """
        path_out = pr8_like.with_name("selected.txt")
        assert frr.main(["--report", str(pr8_like), "--assess", "--path-out", str(path_out)]) == 2
        payload = json.loads(capsys.readouterr().out)
        assert (payload["gate"], payload["critical"]) == ("", 2)
        assert path_out.read_text(encoding="utf-8") == ""

    @pytest.mark.parametrize(
        ("outcome", "readiness"),
        [
            pytest.param("NEEDS_WORK", "complete", id="valid-v1-no-gaps"),
            pytest.param(None, "blocked", id="valid-undecidable-no-gaps"),
        ],
    )
    def test_valid_publication_has_no_gaps(self, tmp_path: Path, outcome: str | None, readiness: str) -> None:
        """A report the strict gate accepts yields no gaps and keeps its normal gate line."""
        report = _publication(tmp_path, outcome, readiness)
        assessment = frr.assess_report(report)
        assert (assessment.contract, assessment.gaps, assessment.gate) == ("v1", [], frr.gate_line(report))

    def test_legacy_report_is_one_degraded_gap(self, tmp_path: Path) -> None:
        """A pre-contract report is readable but carries no candidate, coverage or concern data to consume."""
        report = tmp_path / "review-report.md"
        _write_legacy_report(report)
        assessment = frr.assess_report(report)
        assert (assessment.contract, assessment.gate) == ("legacy", "Gate: BLOCK")
        assert [(gap.rule, gap.severity.value) for gap in assessment.gaps] == [("legacy-contract", "degraded")]

    @pytest.mark.parametrize("gate", ["REJECTED", "reject_scope"])
    def test_unrecognized_legacy_gate_is_critical(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], gate: str
    ) -> None:
        """A legacy gate line resolve cannot act on is an ambiguous verdict, never a consumable legacy report.

        Intake reaches ``--assess`` exactly when the reject gate refused that line, so grading it as the usual degraded
        legacy gap would offer Proceed on a misspelled rejection.
        """
        report = tmp_path / "review-report.md"
        _write_legacy_report(report, gate=gate, outcome="NEEDS_WORK")
        assert frr.main(["--report", str(report), "--assess"]) == 2
        payload = json.loads(capsys.readouterr().out)
        assert (payload["gate"], [(gap["rule"], gap["severity"]) for gap in payload["gaps"]]) == (
            "",
            [("legacy-gate-ambiguous", "critical")],
        )

    @pytest.fixture(name="graded_publication")
    def _graded_publication(self, tmp_path: Path) -> Path:
        """Publish a strictly valid review with one confirmed (published) and one refuted candidate to damage."""
        report = _publication(tmp_path, "NEEDS_WORK")
        data = _read_envelope(tmp_path)
        data["candidates"] = [_named_candidate("backend-one", "CONFIRMED"), _named_candidate("refuted-one", "REFUTED")]
        _write_envelope(tmp_path, data, [finding_row("backend-one")])
        assert frr.gate_line(report) == "Gate: PASS"
        return report

    @pytest.mark.parametrize(
        ("mutate", "gap", "returncode"),
        [
            pytest.param(
                _set(("assessments", 0, "evidence"), []),
                ("assessment-unbacked", "critical", "QA"),
                2,
                id="qa-no-evidence",
            ),
            pytest.param(
                _set(("assessments", 0, "coverage"), ""),
                ("assessment-unbacked", "critical", "QA"),
                2,
                id="qa-no-coverage",
            ),
            pytest.param(
                _set(("assessments", 0, "limits"), 5),
                ("assessment-unbacked", "critical", "QA"),
                2,
                id="limits-not-list",
            ),
            pytest.param(
                _set(("assessments", 0, "source_rating"), 4),
                ("assessment-malformed", "critical", "QA"),
                2,
                id="bad-rating-keeps-role-subject",
            ),
            pytest.param(
                _missing_with_judgment,
                ("assessment-malformed", "critical", "Performance engineer"),
                2,
                id="missing-with-judgment",
            ),
            pytest.param(_blocked_int_role, ("assessment-malformed", "critical", "-"), 2, id="int-role-blocked"),
            pytest.param(
                _set(("assessments", 0, "role"), {"name": "QA"}),
                ("assessment-malformed", "critical", "-"),
                2,
                id="mapping-role-otherwise-valid",
            ),
            pytest.param(
                _set(("candidates", 1, "id"), 5), ("candidate-invalid", "critical", "-"), 2, id="int-candidate-id"
            ),
            pytest.param(
                _set(("candidates", 1, "id"), True), ("candidate-invalid", "critical", "-"), 2, id="bool-candidate-id"
            ),
            pytest.param(
                _set(("candidates", 1, "id"), {"a": 1}),
                ("candidate-invalid", "critical", "-"),
                2,
                id="dict-candidate-id",
            ),
            pytest.param(
                _set(("candidates", 1, "disposition"), ["X"]),
                ("sidecar-malformed", "critical", "-"),
                2,
                id="unhashable-disposition",
            ),
            pytest.param(
                _set(("source_outcome",), ["X"]),
                ("sidecar-malformed", "critical", "-"),
                2,
                id="list-aggregate-outcome",
            ),
            pytest.param(
                _set(("open_concerns",), [{**concern("backend-one"), "candidate_id": ["backend-one"]}]),
                ("concern-unknown-candidate", "critical", "['backend-one']"),
                2,
                id="list-concern-candidate-id",
            ),
            pytest.param(
                _list_finding_candidate,
                ("finding-unbacked", "critical", "['V9']"),
                2,
                id="list-finding-candidate-id",
            ),
            pytest.param(
                _open_concern_under_complete,
                ("readiness-mislabel", "degraded", "open-one"),
                0,
                id="label-only-stays-consumable",
            ),
            pytest.param(
                _label_slip_row_without_change,
                ("finding-unmergeable", "critical", "backend-one"),
                2,
                id="label-slip-row-without-change",
            ),
            pytest.param(
                _label_slip_row_unknown_severity,
                ("finding-unmergeable", "critical", "backend-one"),
                2,
                id="label-slip-unknown-severity",
            ),
            pytest.param(
                _label_slip_rows_sharing_an_id,
                ("finding-unmergeable", "critical", "second-one"),
                2,
                id="label-slip-repeated-finding-id",
            ),
            pytest.param(
                _label_slip_unattributed_row_without_change,
                ("finding-unmergeable", "critical", "-"),
                2,
                id="unattributed-unmergeable-row",
            ),
        ],
    )
    def test_every_strict_rejection_grades_at_least_one_gap(
        self,
        graded_publication: Path,
        capsys: pytest.CaptureFixture[str],
        mutate: Mutation,
        gap: tuple[str, str, str],
        returncode: int,
    ) -> None:
        """Each damage the strict gate refuses is graded as a named gap through ``--assess``, never zero or a crash.

        Regression for the mutation fuzz: unbacked QA sign-off and non-string candidate ids graded zero gaps and became
        consumable, and unhashable or non-string values crashed the grader with a traceback instead of an exit-2 JSON
        line. A record whose role is still a name keeps that role as its subject, so triage can name it in a rerun.
        """
        run = graded_publication.parent
        data, rows = _read_envelope(run), _read_rows(run / "findings.jsonl")
        mutate(data, rows)
        _write_envelope(run, data, rows)
        assert frr.gate_line(graded_publication) == ""
        assert frr.main(["--report", str(graded_publication), "--assess"]) == returncode
        payload = json.loads(capsys.readouterr().out)
        assert gap in [(entry["rule"], entry["severity"], entry["subject"]) for entry in payload["gaps"]]

    def test_unmergeable_row_still_backs_its_candidate(self, graded_publication: Path) -> None:
        """A confirmed row the merge would refuse grades one critical gap naming its candidate, no missing-row gap.

        Regression: a label-slip report whose confirmed row lacked ``change`` graded degraded-only, so resolve offered
        Proceed and its merge then blocked on the row. Naming the candidate lets a targeted rerun re-validate it, which
        replaces the row; a second ``finding-missing`` gap would instead route the candidate to a verbatim row publish.
        """
        run = graded_publication.parent
        data, rows = _read_envelope(run), _read_rows(run / "findings.jsonl")
        _label_slip_row_without_change(data, rows)
        _write_envelope(run, data, rows)
        gaps = [(gap.rule, gap.severity.value, gap.subject) for gap in frr.assess_report(graded_publication).gaps]
        assert gaps == [
            ("readiness-mislabel", "degraded", "open-one"),
            ("finding-unmergeable", "critical", "backend-one"),
        ]

    def test_deeply_nested_evidence_grades_without_a_traceback(
        self, graded_publication: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A label-slip report whose evidence nests 600 lists deep is graded like any label slip, not crashed on.

        The residual check used to deep-copy the envelope and overflow the interpreter stack at this depth, printing a
        traceback where resolve expected one JSON line; it now copies only the levels it corrects.
        """
        run = graded_publication.parent
        data, rows = _read_envelope(run), _read_rows(run / "findings.jsonl")
        _open_concern_under_complete(data, rows)
        data["candidates"][0]["counterevidence"] = [_nest(600)]
        _write_envelope(run, data, rows)
        assert frr.main(["--report", str(graded_publication), "--assess"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [(gap["rule"], gap["subject"]) for gap in payload["gaps"]] == [("readiness-mislabel", "open-one")]

    def test_label_correction_leaves_the_envelope_untouched(self) -> None:
        """The residual check corrects labels on a copy; the graded envelope keeps every original label and record."""
        data = {
            "readiness": "complete",
            "assessments": [{"role": "QA", "presence": "completed", "source_outcome": None, "readiness": "complete"}],
            "candidates": [{"id": "c1", "disposition": "UNVERIFIED", "counterevidence": [["kept"]]}],
            "open_concerns": [],
        }
        original = json.loads(json.dumps(data))
        corrected = frr._label_corrected(data, "blocked")
        assert data == original
        assert (corrected["readiness"], corrected["assessments"][0]["readiness"]) == ("blocked", "blocked")
        assert corrected["candidates"][0]["missing_capability"]
        assert corrected["candidates"][0]["counterevidence"] is data["candidates"][0]["counterevidence"]

    @pytest.mark.parametrize(
        ("outcome", "recorded"),
        [
            pytest.param("REJECT", "REVIEW_V1_REJECT_SCOPE @abcdef1234567", id="rejection-under-critical-gap"),
            pytest.param("NEEDS_WORK", "", id="no-rejection"),
        ],
    )
    def test_echoes_a_recorded_rejection_whatever_the_gaps(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], outcome: str, recorded: str
    ) -> None:
        """``recorded_rejection`` carries the header's ``REJECT_`` gate even when a critical gap empties ``gate``.

        Resolve detected a rejection only through ``gate``, which stays empty under any critical gap (here the only
        reviewer, QA, blocked under a claimed-complete review), so a rejected PR was offered reruns without ever being
        told it was rejected. The exit code stays that of the graded gaps.
        """
        report = _publication(tmp_path, outcome)
        _edit_envelope(tmp_path, _blocked_assessment_under_complete)
        assert frr.main(["--report", str(report), "--assess"]) == 2
        payload = json.loads(capsys.readouterr().out)
        assert (payload["gate"], payload["recorded_rejection"]) == ("", recorded)

    @pytest.mark.parametrize(
        ("readiness", "mutate", "gaps", "gate"),
        [
            pytest.param(
                "blocked",
                _judged_reviewer,
                [("assessment-not-complete", "degraded", "QA"), ("readiness-mislabel", "degraded", "-")],
                "Gate: BLOCK",
                id="aggregate-outcome-slip-is-a-label",
            ),
            pytest.param(
                "incomplete",
                _missing_reviewer,
                [
                    ("assessment-missing", "degraded", "QA"),
                    ("readiness-mislabel", "degraded", "-"),
                    ("ungraded-rule", "critical", "-"),
                ],
                "",
                id="no-reviewer-supports-any-outcome",
            ),
        ],
    )
    def test_null_aggregate_outcome_grades_by_what_the_reviewers_support(
        self, tmp_path: Path, readiness: str, mutate: Callable[[dict], None], gaps: list, gate: str
    ) -> None:
        """A null aggregate outcome a reviewer's judgment can correct is a label; one no reviewer supports is critical.

        Regression: a null aggregate over a reviewer who judged graded a degraded ``readiness-mislabel`` plus a
        critical ``ungraded-rule`` twin for the same rule, because the residual check never relabelled the aggregate
        outcome, so resolve refused to proceed with intact findings. With every reviewer missing, no outcome is
        supportable and no label correction clears the refusal, so the report must never become consumable.
        """
        report = _publication(tmp_path, None, readiness)
        _edit_envelope(tmp_path, mutate)
        assessment = frr.assess_report(report)
        assert frr.gate_line(report) == ""
        assert ([(g.rule, g.severity.value, g.subject) for g in assessment.gaps], assessment.gate) == (gaps, gate)

    @pytest.mark.parametrize("role", ["Security auditor", "Test engineer", "QA specialist"])
    def test_blocked_security_reviewer_is_critical_under_any_qa_name(self, tmp_path: Path, role: str) -> None:
        """Every role name fill-gaps routes to the QA unit is the security reviewer the grader holds critical.

        Role names are free text; a blocked "Security auditor" under a claimed-complete review used to grade degraded
        and offer Proceed, while fill-gaps already re-ran it as the QA specialist.
        """
        report = _publication(tmp_path, "NEEDS_WORK")
        _edit_envelope(tmp_path, lambda data: data.update(assessments=[assessment(role, "blocked")]))
        gaps = [(gap.rule, gap.severity.value, gap.subject) for gap in frr.assess_report(report).gaps]
        assert gaps == [("assessment-not-complete", "critical", role)]

    @mock.patch("find_review_report.current_head_sha", autospec=True)
    @pytest.mark.parametrize(
        ("head_file", "recorded", "current", "head_moved", "calls"),
        [
            pytest.param("head-sha.txt", _HEAD, _HEAD + "0" * 27, False, ["7"], id="same-head-abbreviated"),
            pytest.param("head-sha.txt", _HEAD, "1234567abcdef" + "0" * 27, True, ["7"], id="head-moved"),
            pytest.param("head-sha.txt", _HEAD, "", None, ["7"], id="head-unfetchable"),
            pytest.param("unrelated.txt", _HEAD, _HEAD, None, [], id="no-head-file-no-gh-call"),
            pytest.param("head-sha.txt", "not-a-sha", _HEAD, None, [], id="garbage-head-file-no-gh-call"),
        ],
    )
    def test_reports_whether_the_head_moved(
        self,
        current_head_sha: mock.Mock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        head_file: str,
        recorded: str,
        current: str,
        head_moved: bool | None,
        calls: list[str],
    ) -> None:
        """``--assess`` adds ``head_moved`` so triage never recommends a fill-gaps rerun that would refuse a moved head.

        The PR number comes from the report header when ``--pr`` is absent; ``gh`` is asked only when the run recorded a
        commit id, and a prefix match tolerates the abbreviated id. The exit code stays that of the graded gaps.
        """
        current_head_sha.return_value = current
        report = _publication(tmp_path, "NEEDS_WORK")
        write_lf(tmp_path / head_file, f"{recorded}\n")
        assert frr.main(["--report", str(report), "--assess"]) == 0
        payload = json.loads(capsys.readouterr().out)
        asked = [call.args[0] for call in current_head_sha.call_args_list]
        assert (payload["head_moved"], payload["critical"], asked) == (head_moved, 0, calls)

    @pytest.mark.parametrize(
        ("readiness", "extra", "gate", "reason"),
        [
            pytest.param(
                "incomplete", [_MISSING_REVIEWER], "Gate: BLOCK", "", id="missing-reviewer-makes-null-incomplete"
            ),
            pytest.param("blocked", [], "Gate: BLOCK", "", id="completed-undecidable-stays-blocked"),
            pytest.param(
                "blocked",
                [_MISSING_REVIEWER],
                "",
                "missing assessment 'Performance engineer' requires null judgment and incomplete readiness",
                id="missing-reviewer-under-blocked",
            ),
            pytest.param(
                "incomplete",
                [],
                "",
                "null aggregate outcome requires a completed null assessment in every case",
                id="incomplete-without-missing-reviewer",
            ),
        ],
    )
    def test_missing_reviewer_outranks_blocked_under_a_null_outcome(
        self, tmp_path: Path, readiness: str, extra: list[dict], gate: str, reason: str
    ) -> None:
        """A null aggregate with a missing reviewer publishes as ``incomplete``, and the grader agrees with the gate.

        A null aggregate needs ``blocked`` readiness and a missing reviewer needs ``incomplete``; with both present no
        label was valid, so the review always stopped BLOCKED after its one re-dispatch. A missing required reviewer now
        outranks ``blocked``. The grader mirrors the rule, so a refused envelope still grades at least one gap and an
        accepted one none.
        """
        report = _publication(tmp_path, None, readiness)

        def mutate(data: dict) -> None:
            """Block the only reviewer and append the extra reviewers."""
            data["assessments"][0]["readiness"] = "blocked"
            data["assessments"] += extra

        _edit_envelope(tmp_path, mutate)
        line, why = frr.gate_verdict(report)
        assert line == gate
        assert reason in why
        assert bool(frr.assess_report(report).gaps) is not bool(gate)

    @pytest.mark.parametrize(
        ("readiness", "gate", "reason"),
        [
            pytest.param("incomplete", "Gate: BLOCK", "", id="pending-candidate-makes-null-incomplete"),
            pytest.param(
                "blocked",
                "",
                "otherwise readiness=blocked (pending candidate 'backend-one')",
                id="pending-candidate-under-blocked-is-named",
            ),
        ],
    )
    def test_pending_candidate_outranks_blocked_under_a_null_outcome(
        self, tmp_path: Path, readiness: str, gate: str, reason: str
    ) -> None:
        """A null aggregate beside a pending (null-disposition) candidate publishes as ``incomplete``, not ``blocked``.

        The completed undecidable reviewer requires ``blocked`` and the pending candidate requires ``incomplete``; with
        both present no label was valid, so the review always stopped BLOCKED after its one re-dispatch. Anything still
        owed now outranks ``blocked``, ``blocked`` fails naming the candidate, and the grader agrees with the gate: a
        refused envelope grades at least one gap, never a critical one, and an accepted one none.
        """
        report = _publication(tmp_path, None, readiness)
        data = _read_envelope(tmp_path)
        data["assessments"][0]["readiness"] = "blocked"
        data["candidates"] = [_candidate(None)]
        data["open_concerns"] = [concern("backend-one")]
        _write_envelope(tmp_path, data, [])
        line, why = frr.gate_verdict(report)
        assessment = frr.assess_report(report)
        assert line == gate
        assert reason in why
        assert (bool(assessment.gaps), assessment.critical) == (not gate, False)


class TestProducerGate:
    """``--producer``: the extra rules the review's own gate applies before shipping a publication."""

    @pytest.mark.parametrize(
        ("damage", "returncode", "expected"),
        [
            pytest.param(_no_damage, 0, "[gate] Gate: PASS", id="merge-ready-publication-ships"),
            pytest.param(
                _row_without("author"),
                1,
                "[gate] reason: findings.jsonl finding 1 missing author — run mint_finding_ids.py",
                id="row-without-author",
            ),
            pytest.param(
                _row_without("change"),
                1,
                "[gate] reason: findings.jsonl finding 1 missing change — run mint_finding_ids.py",
                id="row-without-change",
            ),
            pytest.param(
                _row_without("id"),
                1,
                "[gate] reason: findings.jsonl finding 1 missing id — run mint_finding_ids.py",
                id="row-without-id",
            ),
            pytest.param(
                _row_severity("major"),
                1,
                "[gate] reason: findings.jsonl finding 1 has unknown severity 'major' — use one of critical, high, medium",
                id="row-with-unknown-severity",
            ),
            pytest.param(_row_severity(" HIGH "), 0, "[gate] Gate: PASS", id="severity-case-and-spaces-as-merge-reads"),
            pytest.param(
                _second_row_reusing_id,
                1,
                "[gate] reason: findings.jsonl duplicate finding id f-backend-one on findings 1 and 2 — run mint_finding_ids",
                id="rows-sharing-an-id",
            ),
            pytest.param(
                _legacy_shaped_header,
                1,
                "[gate] reason: header lacks Review contract: 1 — the current producer writes the version-one contract",
                id="legacy-shaped-header",
            ),
        ],
    )
    def test_refuses_what_resolve_cannot_consume(
        self,
        confirmed_publication: Path,
        capsys: pytest.CaptureFixture[str],
        damage: Callable[[Path], None],
        returncode: int,
        expected: str,
    ) -> None:
        """``--producer`` refuses a report resolve would accept at intake but then fail to merge, or read as an archive.

        Regression: a version-one publication whose row lacked ``author``, ``change`` or ``id``, carried a severity word
        the merge does not score, or reused another row's id passed the review's own gate, was delivered as finished,
        and resolve's merge then blocked (or, for the repeated id, silently dropped a confirmed finding); a header
        without the version-one markers bypassed every sidecar rule as a legacy report. Severity is compared the way
        the merge reads it, case and surrounding spaces ignored. The failure keeps the intake gate's marker and reason
        line, so the review's gate-failure routing and its one consolidator re-spawn work unchanged.
        """
        damage(confirmed_publication)
        assert frr.main(["--report", str(confirmed_publication), "--producer"]) == returncode
        output = capsys.readouterr().out
        assert expected in output
        assert ("incomplete-review-report" in output) is bool(returncode)

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_row_without("author"), id="row-without-author"),
            pytest.param(_legacy_shaped_header, id="legacy-shaped-header"),
        ],
    )
    def test_intake_without_producer_still_accepts_archives_and_bare_rows(
        self, confirmed_publication: Path, damage: Callable[[Path], None]
    ) -> None:
        """Resolve's intake still reads legacy reports and minimal rows; only the producer gate adds the extra rules."""
        damage(confirmed_publication)
        assert frr.main(["--report", str(confirmed_publication)]) == 0

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--pr", "7", "--producer"], id="pr-lookup"),
            pytest.param(["--report", "r.md", "--assess", "--producer"], id="with-assess"),
        ],
    )
    def test_needs_an_explicit_report_to_gate(self, argv: list[str]) -> None:
        """``--producer`` gates the report the review just wrote; a PR lookup or a gap assessment is a usage error."""
        with pytest.raises(SystemExit) as stop:
            frr.main(argv)
        assert stop.value.code == 2

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_no_damage, id="merge-ready-row"),
            pytest.param(_row_without("author"), id="row-without-author"),
        ],
    )
    def test_keeps_the_reject_route_whatever_the_rows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: Callable[[Path], None]
    ) -> None:
        """A consolidator-published rejection exits 1 on the reject route under ``--producer``, never as incomplete.

        Regression: a ``REJECT_`` publication whose row lacked ``author`` printed the ``incomplete-review-report``
        marker, so the review spent its one consolidator re-spawn repairing the row, republished the same rejection and
        only then reached its rejection handling. The producer never publishes a rejection as finished work, so its row
        rules do not apply to one.
        """
        report = _publication(tmp_path, "REJECT")
        data = _read_envelope(tmp_path)
        data["candidates"] = [_candidate("CONFIRMED")]
        _write_envelope(tmp_path, data, [finding_row("backend-one")])
        damage(report)
        assert frr.main(["--report", str(report), "--producer"]) == 1
        output = capsys.readouterr().out
        assert "rejected (Gate: REJECT_SCOPE @abcdef1234567)" in output
        assert "incomplete-review-report" not in output
