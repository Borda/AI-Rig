"""Exercise malformed audit records, unambiguous lineage, and safe CLI output."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import verify_blueprint_audit as verifier


def _before(**updates) -> dict:
    """Build one independently specified valid before-row with optional replacements."""
    return {
        "record_id": "r",
        "timestamp": "2026-11-11T00:00:00Z",
        "agent_id": "foundry/allow-dispatch",
        "agent_version": "1",
        "session_id": "s",
        "tool_use_id": "t",
        "action_type": "tool.bash",
        "action_detail": {
            "decision": "allow",
            "lane": "blueprint",
            "rank": 1,
            "verdicts": [
                {"lane": "blueprint", "decision": "allow"},
                {"lane": "shape", "decision": "passthrough"},
            ],
        },
        "outcome": "pending",
        "trust_level": "plugin",
        "parent_record_id": None,
        "prev_hash": None,
        "record_phase": "pre_execution",
        "project": None,
        **updates,
    }


def _write_records(path: Path, records: list[dict]) -> Path:
    """Write hash-valid records without using the verifier's hashing implementation."""
    lines = []
    for record in records:
        raw = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        lines.append(json.dumps({**record, "record_hash": hashlib.sha256(raw).hexdigest()}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("action_type", [], id="action-list"),
        pytest.param("record_phase", [], id="phase-list"),
        pytest.param("trust_level", {}, id="trust-object"),
        pytest.param("outcome", [], id="outcome-list"),
    ],
)
def test_unhashable_top_level_fields_warn_and_keep_healthy_rows(tmp_path: Path, key: str, value: object) -> None:
    """A hash-valid malformed row is schema-invalid and cannot discard its healthy neighbour."""
    path = _write_records(tmp_path / "rows.jsonl", [_before(**{key: value}), _before(record_id="healthy")])
    report = verifier.verify_file(path, 100, 1000).as_dict()
    assert report["schema-invalid"] == 1
    assert report["corrupt-record"] == 0
    assert report["counts"]["parent"] == {"s|t": "healthy"}


@pytest.mark.parametrize(
    ("key", "value"),
    [pytest.param("decision", {}, id="decision-object"), pytest.param("lane", [], id="lane-list")],
)
def test_unhashable_before_detail_is_schema_invalid(tmp_path: Path, key: str, value: object) -> None:
    """Unhashable decision and lane values produce a report rather than a TypeError."""
    record = _before()
    record["action_detail"][key] = value
    path = _write_records(tmp_path / "rows.jsonl", [record])
    report = verifier.verify_file(path, 100, 1000).as_dict()
    assert report["schema-invalid"] == 1
    assert report["corrupt-record"] == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [pytest.param("decision", [], id="verdict-decision-list"), pytest.param("lane", {}, id="verdict-lane-object")],
)
def test_unhashable_verdict_is_schema_invalid(tmp_path: Path, key: str, value: object) -> None:
    """An invalid verdict cannot abort classification of the entire log."""
    record = _before()
    record["action_detail"]["verdicts"][0][key] = value
    path = _write_records(tmp_path / "rows.jsonl", [record])
    assert verifier.verify_file(path, 100, 1000).as_dict()["schema-invalid"] == 1


def test_unhashable_after_outcome_is_schema_invalid(tmp_path: Path) -> None:
    """An after-row with an object outcome warns without aborting the report."""
    record = _before(record_phase="post_execution", outcome={}, action_detail={"status": "ok", "event": "PostToolUse"})
    path = _write_records(tmp_path / "rows.jsonl", [record])
    assert verifier.verify_file(path, 100, 1000).as_dict()["schema-invalid"] == 1


@pytest.mark.parametrize("rank", [pytest.param([1], id="list"), pytest.param({"rank": 1}, id="object"), "2"])
def test_mixed_rank_types_preserve_the_lowest_numeric_parent(tmp_path: Path, rank: object) -> None:
    """Non-numeric ranks sort last and numeric strings do not cause mixed-type comparisons."""
    other = _before(record_id="other")
    other["action_detail"]["rank"] = rank
    path = _write_records(tmp_path / "rows.jsonl", [other, _before(record_id="first")])
    assert verifier.verify_file(path, 100, 1000).as_dict()["counts"]["parent"] == {"s|t": "first"}


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        pytest.param(("a|b", "c"), ("a", "b|c"), {"a\\|b|c": "first", "a|b\\|c": "second"}, id="pipe"),
        pytest.param(
            ("a\\", "b|c"),
            ("a\\|b", "c"),
            {"a\\\\|b\\|c": "first", "a\\\\\\|b|c": "second"},
            id="escaped-pipe",
        ),
    ],
)
def test_distinct_groups_never_overwrite_a_parent(
    tmp_path: Path, first: tuple[str, str], second: tuple[str, str], expected: dict[str, str]
) -> None:
    """Tuple join identities survive both report construction and JSON serialization."""
    path = _write_records(
        tmp_path / "rows.jsonl",
        [
            _before(session_id=first[0], tool_use_id=first[1], record_id="first"),
            _before(session_id=second[0], tool_use_id=second[1], record_id="second"),
        ],
    )
    parents = verifier.verify_file(path, 100, 1000).as_dict()["counts"]["parent"]
    assert json.loads(json.dumps(parents)) == expected


def test_text_report_escapes_untrusted_names_and_record_strings() -> None:
    """Control characters in filenames, group identifiers and parents remain visible text."""
    name, group, parent = "bad\x1b]0;title\x07.jsonl", "session\nforged|tool", "parent\x1b[31m"
    data = verifier.FileReport().as_dict()
    data["counts"]["parent"] = {group: parent}
    text = verifier.render_text({name: data}, data, 0)
    assert ascii(name) in text
    assert ascii(group) in text
    assert ascii(parent) in text
    assert "\x1b" not in text
    assert "\x07" not in text
    assert "session\nforged" not in text


def test_missing_path_diagnostic_escapes_terminal_sequences(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Error diagnostics cannot execute a terminal sequence embedded in a supplied path."""
    path = tmp_path / "missing\x1b[31m.jsonl"
    assert verifier.main(["verify", str(path)]) == 2
    error = capsys.readouterr().err
    assert ascii(str(path)) in error
    assert "\x1b" not in error


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["--keep-session", "\ud800"], id="non-utf8-session"),
        pytest.param(["--older-than", "9" * 309], id="overflowing-age"),
        pytest.param(["--older-than", "365001"], id="age-outside-supported-range"),
    ],
)
def test_invalid_prune_arguments_fail_before_removing_files(tmp_path: Path, arguments: list[str]) -> None:
    """Invalid session encoding and oversized ages return CLI usage errors without deleting anything."""
    path = tmp_path / ("s-" + "a" * 32 + ".jsonl")
    path.write_text("old\n", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        verifier.main(["prune", str(tmp_path), *arguments])
    assert error.value.code == 2
    assert path.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_nonpositive_line_limit_is_a_usage_error(tmp_path: Path, limit: str) -> None:
    """A nonpositive cap cannot report a misleading scan over no records."""
    with pytest.raises(SystemExit) as error:
        verifier.main(["verify", str(tmp_path), "--max-lines", limit])
    assert error.value.code == 2


def test_default_scan_reports_a_prefix_at_one_hundred_thousand_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default bounds retained rows and explicitly reports an incomplete scan."""
    (tmp_path / "large.jsonl").write_text("null\n" * 100_001, encoding="utf-8")
    assert verifier.main(["verify", str(tmp_path), "--json"]) == 0
    totals = json.loads(capsys.readouterr().out)["totals"]
    assert totals["schema-invalid"] == 100_000
    assert totals["limit-exceeded"] == 1
