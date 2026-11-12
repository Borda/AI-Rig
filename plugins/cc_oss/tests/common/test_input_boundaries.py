"""Regression checks for whole-value inputs and private workflow outputs."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path

import build_vitality_paths as vitality_paths
import classify_breaking
import codemap_batch
import evolution_score
import fetch_gh_data_group1 as group1
import fetch_gh_data_group2 as group2
import fill_gaps_scope
import find_review_report
import gh_batch_query
import merge_action_items
import pytest
import vitality_extract


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_vitality_jsonl_preserves_string_separators(separator: str) -> None:
    """Only LF delimits JSONL records; Unicode separators remain string content."""
    record = {"type": "readme_content", "data": f"before{separator}after"}
    assert vitality_extract.load_records(json.dumps(record, ensure_ascii=False) + "\n") == {"readme_content": record}


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_review_findings_preserve_string_separators(tmp_path: Path, separator: str) -> None:
    """Review sidecar records preserve the same JSON string content as the producer wrote."""
    record = {"title": f"before{separator}after"}
    (tmp_path / "findings.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    assert find_review_report._read_findings(tmp_path / "review-report.md") == [record]


def test_graphql_rejects_sha_with_final_newline() -> None:
    """GraphQL must never interpolate a partially validated hash."""
    with pytest.raises(ValueError, match="not a full commit hash"):
        gh_batch_query.build_commit_query(["a" * 40 + "\n"])


def test_graphql_rejects_repository_with_final_newline(tmp_path: Path) -> None:
    """Explicit repository arguments must match as complete values."""
    with pytest.raises(ValueError, match="not OWNER/NAME"):
        gh_batch_query.resolve_repo("owner/repo\n", tmp_path)


def test_codemap_rejects_module_with_final_newline() -> None:
    """Module validation cannot accept an unmatched final newline."""
    assert not codemap_batch._valid_module("package.module\n")


def test_group_one_rejects_repository_with_final_newline() -> None:
    """The fetcher rejects malformed names before dispatching GitHub calls."""
    assert group1._parse_args(["--repo", "owner/repo\n", "--output-dir", "out"])[1] is not None


@pytest.mark.parametrize(
    ("owner", "repo", "branch"),
    [
        pytest.param("owner\n", "repo", "main", id="owner"),
        pytest.param("owner", "repo\n", "main", id="repo"),
        pytest.param("owner", "repo", "main\n", id="branch"),
    ],
)
def test_group_two_rejects_names_with_final_newline(owner: str, repo: str, branch: str) -> None:
    """Every GitHub path component must match the entire supplied value."""
    assert group2._validate_args(owner, repo, branch, "out") is not None


def test_review_run_sort_rejects_partial_name() -> None:
    """A newline-suffixed directory must not acquire a valid run number."""
    assert find_review_report._run_sort_key(Path("run-999\n")) == (-1, "run-999\n")


def test_vitality_sentinel_replaces_existing_inode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing state replaces a hardlinked entry without modifying its original target."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "private")
    original = tmp_path / "original"
    original.write_text("keep", encoding="utf-8")
    sentinel = tmp_path / "analyse-report-file-private"
    os.link(original, sentinel)
    vitality_paths._write_sentinel("analyse-report-file", "report")
    assert original.read_text(encoding="utf-8") == "keep"
    assert sentinel.read_bytes() == b"report\n"


def test_merge_sentinel_replaces_existing_inode(tmp_path: Path) -> None:
    """The selected findings pointer cannot overwrite a hardlinked unrelated file."""
    report = tmp_path / "review-report.md"
    findings = tmp_path / "findings.jsonl"
    findings.write_text("{}\n", encoding="utf-8")
    original = tmp_path / "original"
    original.write_text("keep", encoding="utf-8")
    sentinel = tmp_path / "sentinel"
    os.link(original, sentinel)
    args = argparse.Namespace(
        report_file=str(report),
        impl_dir=str(tmp_path),
        persist_findings=str(sentinel),
        intake_accepted="",
        accept_degraded=False,
    )
    assert merge_action_items._select_findings(args) == 0
    assert original.read_text(encoding="utf-8") == "keep"
    assert sentinel.read_text(encoding="utf-8") == f"{findings.as_posix()}\n"


@pytest.mark.parametrize("output", ["plan", "head"])
def test_gap_outputs_replace_existing_inode(tmp_path: Path, output: str) -> None:
    """Both plan pointers replace planted files before publishing or clearing state."""
    original = tmp_path / "original"
    original.write_text("keep", encoding="utf-8")
    os.link(original, tmp_path / output)
    fill_gaps_scope._write_outputs(tmp_path / "plan", tmp_path / "head", {"head_sha": "abc1234"})
    assert original.read_text(encoding="utf-8") == "keep"
    assert (tmp_path / "head").read_bytes() == b"abc1234\n"


def test_score_atomic_output_ignores_predictable_temp(tmp_path: Path) -> None:
    """A planted old-style temporary name is never opened or replaced."""
    output = tmp_path / "rows.jsonl"
    original = tmp_path / "original"
    original.write_text("keep", encoding="utf-8")
    os.link(original, output.with_name(f".{output.name}.{os.getpid()}.tmp"))
    evolution_score._write_jsonl(output, [{"value": 1}])
    assert original.read_text(encoding="utf-8") == "keep"
    assert output.read_bytes() == b'{"value":1}\n'


def test_score_rejects_oversized_json(tmp_path: Path) -> None:
    """Oversized score inputs fail before JSON parsing or unbounded allocation."""
    source = tmp_path / "input.json"
    with source.open("wb") as handle:
        handle.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="size limit"):
        evolution_score._read_json(source)


def test_breaking_classifier_bounds_stdin(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """An oversized valid document returns the classifier's input-error status."""
    source = io.StringIO(" " * (10 * 1024 * 1024) + "{}")
    monkeypatch.setattr(classify_breaking.sys, "stdin", source)
    assert classify_breaking.main([]) == 2
    assert "size limit" in json.loads(capsys.readouterr().out)["error"]
    assert source.tell() == 10 * 1024 * 1024 + 1


@pytest.mark.parametrize(
    ("payload", "status", "message"),
    [
        pytest.param(b"{}" + b" " * (10 * 1024 * 1024 - 2), 0, "", id="exact-byte-ceiling"),
        pytest.param(b" " * (10 * 1024 * 1024) + b"{}", 2, "size limit", id="over-byte-ceiling"),
        pytest.param(b"\xff", 2, "not valid JSON", id="invalid-utf8"),
    ],
)
def test_breaking_classifier_reads_bounded_binary_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, payload: bytes, status: int, message: str
) -> None:
    """The real stdin buffer applies byte bounds and reports undecodable data through exit two."""
    source = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
    monkeypatch.setattr(classify_breaking.sys, "stdin", source)
    assert classify_breaking.main([]) == status
    assert message in json.loads(capsys.readouterr().out).get("error", "")
    assert source.buffer.tell() == min(len(payload), 10 * 1024 * 1024 + 1)
