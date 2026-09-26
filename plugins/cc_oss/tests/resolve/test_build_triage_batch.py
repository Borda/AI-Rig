"""Tests for ``bin/build_triage_batch.py`` — thread identifiers → codemap-py query batch spec.

The module maps extracted candidate identifiers to ``codemap-py query batch`` queries (dotted modules → ``rdeps``, bare
symbols → ``find-symbol``) and writes the JSON array to an output file for the oss:analyse stale-symbol check.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import build_triage_batch  # type: ignore[import-not-found]


def test_build_queries_module_vs_symbol() -> None:
    """Dotted name → rdeps; bare name → anchored find-symbol."""
    out = build_triage_batch.build_queries(["pkg.mod.sub", "MyClass"])
    assert out == [
        {"cmd": "rdeps", "args": ["pkg.mod.sub", "--exclude-tests"]},
        {"cmd": "find-symbol", "args": ["^MyClass$", "--limit", "1"]},
    ]


def test_build_queries_skips_blank_lines() -> None:
    """Empty / whitespace-only entries are dropped."""
    assert build_triage_batch.build_queries(["  ", "", "x"]) == [
        {"cmd": "find-symbol", "args": ["^x$", "--limit", "1"]},
    ]


def test_build_queries_dotted_but_non_identifier_first_segment() -> None:
    """A leading dot (invalid Python name) falls back to symbol lookup."""
    out = build_triage_batch.build_queries([".hidden"])
    assert out[0]["cmd"] == "find-symbol"


def test_main_missing_args_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """Wrong arg count → exit 1 with usage on stderr."""
    rc = build_triage_batch.main(["only-one"])
    assert rc == 1
    assert "Usage" in capsys.readouterr().err


def test_main_unreadable_candidate_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Missing candidate file → exit 1, error on stderr."""
    rc = build_triage_batch.main([str(tmp_path / "nope.txt"), str(tmp_path / "out.json")])
    assert rc == 1
    assert "cannot read" in capsys.readouterr().err


def test_main_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Candidate file → out file holds the mapped batch JSON; count printed to stdout."""
    cand = tmp_path / "cand.txt"
    cand.write_text("a.b.c\nWidget\n")
    out = tmp_path / "batch.json"
    assert build_triage_batch.main([str(cand), str(out)]) == 0
    assert capsys.readouterr().out.strip() == "2"
    data = json.loads(out.read_text())
    assert [q["cmd"] for q in data] == ["rdeps", "find-symbol"]


def test_main_empty_file_writes_empty_array(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Empty candidate file → out file is ``[]`` and stdout count is ``0`` (caller skips codemap-py query)."""
    cand = tmp_path / "cand.txt"
    cand.write_text("")
    out = tmp_path / "batch.json"
    assert build_triage_batch.main([str(cand), str(out)]) == 0
    assert capsys.readouterr().out.strip() == "0"
    assert json.loads(out.read_text()) == []


def test_golden_invocation_two_positionals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Documented call site ``build_triage_batch.py CANDIDATE_FILE OUT_FILE`` succeeds."""
    cand = tmp_path / "_CAND"
    cand.write_text("pkg.mod\nSymbol\n")
    out = tmp_path / "_BATCH"
    assert build_triage_batch.main([str(cand), str(out)]) == 0
    assert capsys.readouterr().out.strip() == "2"
    assert [q["cmd"] for q in json.loads(out.read_text())] == ["rdeps", "find-symbol"]
