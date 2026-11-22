"""Tests for ``bin/code_safety_status.py``.

They cover when review may skip its PR-code question after one scan and when resolve may skip it after two.
"""

from __future__ import annotations

import json
from pathlib import Path

import code_safety_status as css
import pytest

_HEAD = "a" * 40
_OTHER = "b" * 40


def _status(**over: object) -> dict[str, object]:
    """Return a clean fork-PR sidecar record, with ``over`` applied."""
    return {"schema": 1, "origin": "fork", "scanned": True, "head": _HEAD, "risk_level": "none", **over}


def _scan(**over: object) -> dict[str, object]:
    """Return a clean guard scan report from a host with a usable sandbox, with ``over`` applied."""
    return {
        "head": _HEAD,
        "base": _OTHER,
        "whole_tree": False,
        "digest": "d" * 64,
        "risk": {"level": "none"},
        "verdict": "no-findings",
        "isolation_available": "sandbox",
        **over,
    }


_REVIEW_CASES = pytest.mark.parametrize(
    ("scan", "head", "expected"),
    [
        pytest.param(_scan(), _HEAD, "auto", id="full-match"),
        pytest.param(_scan(head="c" * 64), "c" * 64, "auto", id="sha256-object-id"),
        pytest.param(None, _HEAD, "ask reason=no-scan", id="unavailable-scan"),
        pytest.param({}, _HEAD, "ask reason=no-scan", id="missing-scan-head"),
        pytest.param(_scan(), _OTHER, "ask reason=head-mismatch", id="different-head"),
        pytest.param(_scan(head="aaaaaaa"), "aaaaaaa", "ask reason=head-mismatch", id="abbreviated-head"),
        pytest.param(_scan(head=_HEAD.upper()), _HEAD.upper(), "ask reason=head-mismatch", id="uppercase-head"),
        pytest.param(_scan(risk={"level": "borderline"}), _HEAD, "ask reason=findings-present", id="finding"),
        pytest.param(_scan(risk={}), _HEAD, "ask reason=findings-present", id="missing-risk-level"),
        pytest.param({"head": _HEAD}, _HEAD, "ask reason=no-scan", id="missing-scope-and-risk"),
        pytest.param(
            {"head": _HEAD, "risk": {"level": "none"}}, _HEAD, "ask reason=no-scan", id="minimal-head-and-risk-only"
        ),
        pytest.param(_scan(whole_tree=None), _HEAD, "ask reason=no-scan", id="scope-not-boolean"),
        pytest.param(_scan(base=None), _HEAD, "ask reason=no-scan", id="base-scan-without-base"),
        pytest.param(_scan(base="abc"), _HEAD, "ask reason=no-scan", id="abbreviated-base"),
        pytest.param(_scan(whole_tree=True, base=None), _HEAD, "auto", id="whole-tree-needs-no-base"),
        pytest.param(_scan(digest=""), _HEAD, "ask reason=no-scan", id="empty-digest"),
        pytest.param(_scan(digest=None), _HEAD, "ask reason=no-scan", id="missing-digest"),
        pytest.param(_scan(risk=[]), _HEAD, "ask reason=findings-present", id="risk-not-a-dict"),
        pytest.param(_scan(risk={"level": None}), _HEAD, "ask reason=findings-present", id="level-none-value"),
        pytest.param(_scan(risk={"level": "NONE"}), _HEAD, "ask reason=findings-present", id="level-uppercase"),
        pytest.param(_scan(risk={"level": "none "}), _HEAD, "ask reason=findings-present", id="level-trailing-space"),
    ],
)


@_REVIEW_CASES
def test_decide_scan(scan: dict[str, object] | None, head: str, expected: str) -> None:
    """Review skips the question only for a clean scan of the full requested head."""
    assert css.decide_scan(scan, head) == expected


@_REVIEW_CASES
def test_check_scan_prints_decision(
    scan: dict[str, object] | None, head: str, expected: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The review CLI prints exactly one decision line and exits successfully."""
    scan_file = tmp_path / "scan.json"
    scan_file.write_text(json.dumps(scan), encoding="utf-8")
    assert css.main(["check-scan", "--scan", str(scan_file), "--head", head]) == 0
    assert capsys.readouterr() == (expected + "\n", "")


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not json", id="invalid-json"),
        pytest.param("[]", id="not-an-object"),
        pytest.param("[" * 2000 + "]" * 2000, id="over-nested-json"),
    ],
)
def test_check_scan_asks_for_invalid_files(tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str) -> None:
    """Unparsable input and non-objects both produce the no-scan decision."""
    scan_file = tmp_path / "scan.json"
    scan_file.write_text(content, encoding="utf-8")
    assert css.main(["check-scan", "--scan", str(scan_file), "--head", _HEAD]) == 0
    assert capsys.readouterr() == ("ask reason=no-scan\n", "")


def test_check_scan_asks_for_unreadable_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A directory passed as the scan file fails closed with a single decision."""
    assert css.main(["check-scan", "--scan", str(tmp_path), "--head", _HEAD]) == 0
    assert capsys.readouterr() == ("ask reason=no-scan\n", "")


@pytest.mark.parametrize(
    ("status", "scan", "head", "expected"),
    [
        pytest.param(_status(), _scan(), _HEAD, "auto", id="full-match-auto"),
        pytest.param(None, _scan(), _HEAD, "ask reason=no-review-status", id="missing-status"),
        pytest.param(_status(schema=2), _scan(), _HEAD, "ask reason=no-review-status", id="schema-mismatch"),
        pytest.param(_status(origin="same-repo"), _scan(), _HEAD, "ask reason=review-did-not-scan", id="same-repo"),
        pytest.param(_status(scanned=False), _scan(), _HEAD, "ask reason=review-did-not-scan", id="unscanned"),
        pytest.param(_status(), None, _HEAD, "ask reason=no-fresh-scan", id="no-fresh-scan"),
        pytest.param(_status(), _scan(head=_OTHER), _HEAD, "ask reason=head-mismatch", id="fresh-scan-other-head"),
        pytest.param(_status(head=_OTHER), _scan(), _HEAD, "ask reason=head-mismatch", id="review-other-head"),
        pytest.param(_status(), _scan(), _OTHER, "ask reason=head-mismatch", id="pr-head-moved"),
        pytest.param(
            _status(risk_level="borderline"), _scan(), _HEAD, "ask reason=findings-present", id="review-borderline"
        ),
        pytest.param(_status(), _scan(risk={"level": "high"}), _HEAD, "ask reason=findings-present", id="fresh-high"),
        pytest.param(_status(), _scan(risk={}), _HEAD, "ask reason=findings-present", id="fresh-risk-missing"),
        pytest.param(
            _status(), _scan(isolation_available="env-only"), _HEAD, "ask reason=no-sandbox", id="host-without-sandbox"
        ),
        pytest.param(
            _status(), _scan(isolation_available="none"), _HEAD, "ask reason=no-sandbox", id="host-without-isolation"
        ),
        pytest.param(
            _status(),
            _scan(isolation_available="host-sandbox"),
            _HEAD,
            "ask reason=no-sandbox",
            id="host-sandbox-not-probed",
        ),
        pytest.param(_status(), _scan(isolation_available=None), _HEAD, "ask reason=no-sandbox", id="isolation-null"),
        pytest.param(
            _status(),
            _scan(isolation_available="Sandbox"),
            _HEAD,
            "ask reason=no-sandbox",
            id="isolation-wrong-case",
        ),
        pytest.param(
            _status(risk_level="borderline"),
            _scan(isolation_available="env-only"),
            _HEAD,
            "ask reason=findings-present",
            id="findings-reported-before-the-missing-sandbox",
        ),
        pytest.param(
            _status(head="aaaaaaa"), _scan(head="aaaaaaa"), "aaaaaaa", "ask reason=head-mismatch", id="abbreviated-head"
        ),
        pytest.param(
            _status(head=_HEAD.upper()),
            _scan(head=_HEAD.upper()),
            _HEAD.upper(),
            "ask reason=head-mismatch",
            id="upper-hex",
        ),
        pytest.param(_status(schema=True), _scan(), _HEAD, "ask reason=no-review-status", id="schema-true"),
        pytest.param(_status(schema=1.0), _scan(), _HEAD, "ask reason=no-review-status", id="schema-float"),
        pytest.param(_status(), _scan(), "", "ask reason=head-mismatch", id="empty-pr-head"),
        pytest.param(_status(head="c" * 64), _scan(head="c" * 64), "c" * 64, "auto", id="sha256-object-id"),
    ],
)
def test_decide(status: dict[str, object] | None, scan: dict[str, object] | None, head: str, expected: str) -> None:
    """Only a complete clean match skips the question."""
    assert css.decide(status, scan, head) == expected


@pytest.mark.parametrize(
    "scanned",
    [
        pytest.param("yes", id="truthy-string"),
        pytest.param("no", id="string-no"),
        pytest.param(1, id="integer-one"),
        pytest.param([True], id="non-empty-list"),
        pytest.param(None, id="null"),
    ],
)
def test_decide_asks_for_a_scanned_value_that_is_not_the_boolean_true(scanned: object) -> None:
    """Only the boolean ``true`` counts as scanned; a truthy non-boolean never skips the question."""
    assert css.decide(_status(scanned=scanned), _scan(), _HEAD) == "ask reason=review-did-not-scan"


@pytest.mark.parametrize(
    ("status", "scan"),
    [
        pytest.param({k: v for k, v in _status().items() if k != "schema"}, _scan(), id="status-without-schema"),
        pytest.param({k: v for k, v in _status().items() if k != "origin"}, _scan(), id="status-without-origin"),
        pytest.param({k: v for k, v in _status().items() if k != "scanned"}, _scan(), id="status-without-scanned"),
        pytest.param({k: v for k, v in _status().items() if k != "head"}, _scan(), id="status-without-head"),
        pytest.param(
            {k: v for k, v in _status().items() if k != "risk_level"}, _scan(), id="status-without-risk-level"
        ),
        pytest.param(_status(), {k: v for k, v in _scan().items() if k != "head"}, id="scan-without-head"),
        pytest.param(_status(), {k: v for k, v in _scan().items() if k != "risk"}, id="scan-without-risk"),
        pytest.param(
            _status(), {k: v for k, v in _scan().items() if k != "isolation_available"}, id="scan-without-isolation"
        ),
    ],
)
def test_decide_asks_when_any_input_is_absent(status: dict[str, object], scan: dict[str, object]) -> None:
    """A missing field is never defaulted to the passing value: every skip-rule input must be present to skip."""
    assert css.decide(status, scan, _HEAD).startswith("ask reason=")


def test_write_scan_then_check_auto(tmp_path: Path) -> None:
    """A clean scan written as a sidecar lets a clean fresh scan of the same head answer ``auto``."""
    scan_file, out = tmp_path / "scan.json", tmp_path / "code-safety.json"
    scan_file.write_text(json.dumps(_scan(base=_OTHER, digest="d1")), encoding="utf-8")
    assert css.main(["write", "--scan", str(scan_file), "--out", str(out)]) == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["risk_level"] == "none"
    assert record["scan_digest"] == "d1"
    assert css.main(["check", "--status", str(out), "--scan", str(scan_file), "--head", _HEAD]) == 0
    assert css.decide(record, json.loads(scan_file.read_text(encoding="utf-8")), _HEAD) == "auto"


def test_check_prints_decision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``check`` prints the single decision line on stdout."""
    scan_file, out = tmp_path / "scan.json", tmp_path / "code-safety.json"
    scan_file.write_text(json.dumps(_scan()), encoding="utf-8")
    css.main(["write", "--scan", str(scan_file), "--out", str(out)])
    css.main(["check", "--status", str(out), "--scan", str(scan_file), "--head", _HEAD])
    assert capsys.readouterr().out == "auto\n"


@pytest.mark.parametrize(
    ("flags", "origin"),
    [
        pytest.param(["--same-repo"], "same-repo", id="same-repo"),
        pytest.param(["--unscanned"], "fork", id="unscanned-fork"),
    ],
)
def test_write_without_scan_never_asks_skip(tmp_path: Path, flags: list[str], origin: str) -> None:
    """A same-repo or unscanned record never satisfies the skip condition."""
    out, scan_file = tmp_path / "code-safety.json", tmp_path / "scan.json"
    scan_file.write_text(json.dumps(_scan()), encoding="utf-8")
    assert css.main(["write", "--head", _HEAD, *flags, "--out", str(out)]) == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert (record["origin"], record["scanned"]) == (origin, False)
    assert css.decide(record, _scan(), _HEAD).startswith("ask")


@pytest.mark.parametrize(
    "content",
    [pytest.param(None, id="missing"), pytest.param("not json", id="corrupt"), pytest.param("[]", id="not-object")],
)
def test_check_fails_closed_on_unreadable_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None
) -> None:
    """A status file that is missing, corrupt or not an object asks."""
    status, scan_file = tmp_path / "code-safety.json", tmp_path / "scan.json"
    scan_file.write_text(json.dumps(_scan()), encoding="utf-8")
    if content is not None:
        status.write_text(content, encoding="utf-8")
    css.main(["check", "--status", str(status), "--scan", str(scan_file), "--head", _HEAD])
    assert capsys.readouterr().out == "ask reason=no-review-status\n"


def test_write_rejects_non_report(tmp_path: Path) -> None:
    """A ``--scan`` file that is not a guard report is refused with exit 2."""
    bad = tmp_path / "scan.json"
    bad.write_text("{}", encoding="utf-8")
    assert css.main(["write", "--scan", str(bad), "--out", str(tmp_path / "out.json")]) == 2


def test_write_records_an_automatic_run_decision(tmp_path: Path) -> None:
    """An unsandboxed run that no user decided is recorded as automatic beside the scan evidence."""
    scan_file, out = tmp_path / "scan.json", tmp_path / "code-safety.json"
    scan_file.write_text(json.dumps(_scan()), encoding="utf-8")
    assert css.main(["write", "--scan", str(scan_file), "--run-decision", "auto", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["run_decision"] == "auto"
    assert css.main(["write", "--scan", str(scan_file), "--out", str(out)]) == 0
    assert "run_decision" not in json.loads(out.read_text(encoding="utf-8"))
