"""Regression checks for bounded and malformed evolution inputs."""

from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path
from unittest import mock

import _evolution_common as common
import check_oss_pr_signals as signals
import evolution_changelog as changelog
import evolution_releases as releases
import pytest
from evolution_changelog import discover_changelog, parse_changelog
from evolution_eval import main


def test_entry_normalization_truncates_oversized_line() -> None:
    """Unmatched brackets remain bounded without aborting the scan or dropping later lines."""
    assert common.normalise_entry_text("[" * 10001 + "\nFix **next** (#12)") == "[" * 10000 + " fix next"


def test_entry_references_do_not_span_lines() -> None:
    """Unterminated references on short lines cannot repeatedly scan the rest of the entry."""
    assert common.normalise_entry_text("[#12](\n[#12](\n[#12](\n)") == "[#12]( [#12]( [#12]( )"


def test_changelog_truncates_oversized_heading() -> None:
    """An oversized heading remains parseable while its matching input is bounded."""
    assert parse_changelog("## 1.0.0" + " " * 10001 + "\n### Fixed\n- Repair\n", "md") == {
        "1.0.0": {"added": [], "changed": [], "fixed": ["Repair"]}
    }


def test_changelog_preserves_long_ordinary_entry() -> None:
    """Ordinary long release notes remain intact under the parser's bounded line ceiling."""
    entry = "Fix robustly " * 300
    parsed = parse_changelog("## 1.0.0\n### Fixed\n- " + entry, "md")
    assert parsed["1.0.0"]["fixed"] == [entry.rstrip()]
    assert common.normalise_entry_text(entry) == entry.rstrip().lower()


@pytest.mark.integration
def test_changelog_scan_warns_and_keeps_truncated_entry(evo_repo) -> None:
    """A real release scan retains bounded text and surfaces truncation in its result warnings."""
    evo_repo.write("CHANGELOG.md", "## 1.0.0\n### Fixed\n- " + "x" * 10001 + "\n")
    evo_repo.commit("fix: repair", day=0)
    evo_repo.tag("1.0.0", day=1)
    result = changelog.build_entries(evo_repo.path, releases.build_release_index(evo_repo.path), gh_available=False)
    assert result["fixes"][0]["text"] == "x" * 9998
    assert "changelog: truncated 1 line(s) to 10000 characters" in result["warnings"]


@pytest.mark.parametrize(
    ("heading", "text"),
    [
        pytest.param("## C#", "C#", id="literal-hash"),
        pytest.param("## v1.0 ##", "v1.0", id="closing-hashes"),
        pytest.param("## C#  ", "C#", id="literal-hash-trailing-space"),
    ],
)
def test_markdown_heading_preserves_attached_hash(heading: str, text: str) -> None:
    """Only whitespace-separated closing hashes are removed from heading text."""
    assert changelog._md_events([heading]) == [("heading", 2, text)]


@pytest.mark.integration
def test_untagged_repository_reports_no_release_tag(evo_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real Git describe exit 128 in an untagged repository is a normal inventory result."""
    evo_repo.write("src/__init__.py", "Export = 1\n")
    evo_repo.commit("initial", day=0)
    monkeypatch.chdir(evo_repo.path)
    result = signals.collect_signals(
        "42",
        "",
        5,
        "unused-gh",
        "git",
        diff_text="diff --git a/src/__init__.py b/src/__init__.py\n-Export = 1\n",
    )
    assert result.latest_tag == ""
    assert result.check_status["DEPRECATION_CHECK"] == "ok"
    assert result.deprecation_findings == ["NO_RELEASE_TAG: cannot determine release history — skip deprecation check"]


def test_safe_component_rejects_final_newline() -> None:
    """A component must match in full rather than before a final newline."""
    assert not common.safe_component("repo\n")


def test_commit_sha_rejects_final_newline() -> None:
    """A commit identifier must not accept an unmatched trailing newline."""
    with pytest.raises(ValueError, match="invalid commit SHA"):
        changelog._checked_sha("a" * 40 + "\n")
    assert changelog._oid({"oid": "a" * 40 + "\n"}) is None


def test_fragment_filename_rejects_final_newline() -> None:
    """A fragment filename must match its full type and extension."""
    assert changelog._FRAGMENT_NAME.match("12.bugfix\n") is None


@pytest.mark.parametrize("reader", ["read_jsonl", "repair_tail"])
def test_store_read_refuses_oversized_file(tmp_path: Path, reader: str) -> None:
    """Store reads reject oversized files without consuming their contents."""
    path = tmp_path / "rows.jsonl"
    with path.open("wb") as handle:
        handle.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="size limit"):
        getattr(common, reader)(path)


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_store_text_read_preserves_universal_newlines(tmp_path: Path, newline: str) -> None:
    """Bounded text reads preserve the previous reader's cross-platform newline handling."""
    path = tmp_path / "rows.jsonl"
    path.write_bytes(('{"fix_id":"a"}' + newline).encode())
    assert common.read_jsonl(path) == ([{"fix_id": "a"}], 0)


def test_discovery_refuses_oversized_config(tmp_path: Path) -> None:
    """Repository configuration is bounded before towncrier parsing."""
    with (tmp_path / "pyproject.toml").open("wb") as handle:
        handle.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="size limit"):
        discover_changelog(tmp_path)


@pytest.mark.parametrize("scheme", ["file", "ftp", "http", "git+ssh", "ssh+git"])
def test_remote_identity_preserves_other_schemes(scheme: str) -> None:
    """Store identity is independent of which transports the Git runner permits."""
    assert common.parse_remote_url(f"{scheme}://user@Host/owner/repo.git") == ["host", "owner", "repo"]


def test_remote_parser_rejects_external_command() -> None:
    """An external command remote cannot supply a safe host and repository identity."""
    assert common.parse_remote_url("ext::command") is None


def test_jsonl_bare_cr_tail_agrees_with_repair(tmp_path: Path) -> None:
    """The reader drops a bare-CR tail that append-time repair would discard."""
    path = tmp_path / "rows.jsonl"
    tail = b'{"fix_id":"torn"}\r'
    path.write_bytes(b'{"fix_id":"complete"}\r\n' + tail)
    assert common.read_jsonl(path) == ([{"fix_id": "complete"}], 1)
    assert common.repair_tail(path) == len(tail)
    assert common.read_jsonl(path) == ([{"fix_id": "complete"}], 0)


@pytest.mark.parametrize(
    "cell",
    [pytest.param({}, id="missing-column"), pytest.param(None, id="null-cell"), pytest.param(3, id="scalar-cell")],
)
def test_evaluator_reports_malformed_cell_as_exit_two(tmp_path: Path, cell: object) -> None:
    """Malformed scored cells obey the CLI input-error contract without traceback."""
    labels = tmp_path / "labels.csv"
    labels.write_text("repo,fix_id,column_id\nB,f,c\n", encoding="utf-8")
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"fix_id": "f", "cells": [cell]}]), encoding="utf-8")
    assert main(["evaluate", "--labels", str(labels), "--rows", str(rows)]) == 2


def test_evaluator_reports_csv_error_as_exit_two(tmp_path: Path) -> None:
    """CSV parser failures remain ordinary invalid-input results."""
    labels = tmp_path / "labels.csv"
    labels.write_text("repo,fix_id,column_id\nB," + "x" * (csv.field_size_limit() + 1) + ",c\n", encoding="utf-8")
    rows = tmp_path / "rows.json"
    rows.write_text("[]", encoding="utf-8")
    assert main(["evaluate", "--labels", str(labels), "--rows", str(rows)]) == 2


@pytest.mark.parametrize("command", ["evaluate", "tune"])
def test_evaluator_refuses_excessive_resampling(tmp_path: Path, capsys: pytest.CaptureFixture, command: str) -> None:
    """The CLI refuses a bootstrap budget above its documented ceiling."""
    labels = tmp_path / "labels.csv"
    labels.write_text("repo,fix_id,column_id\nB,f,none\n", encoding="utf-8")
    rows = tmp_path / "rows.json"
    rows.write_text("[]", encoding="utf-8")
    argv = [command, "--labels", str(labels), "--rows", str(rows), "--resamples", "10001"]
    assert main([*argv, "--candidates", "0.5"] if command == "tune" else argv) == 2
    assert "resamples must be between 1 and 10000" in capsys.readouterr().err


def test_git_clone_disallows_file_transport(evo_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inherited protocol permissions cannot enable local file cloning through the shared runner."""
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    with pytest.raises(common.GitError, match="transport 'file' not allowed"):
        common.run_git(tmp_path, "clone", evo_repo.path.as_uri(), str(tmp_path / "clone"))


@mock.patch("_evolution_common.subprocess.run", autospec=True)
@pytest.mark.parametrize("allowed", ["https", "https:file", "", "file"])
def test_git_runner_preserves_stricter_protocols(
    run: mock.Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed: str
) -> None:
    """The runner cannot re-enable a protocol excluded by the inherited allow-list."""
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", allowed)
    run.return_value = subprocess.CompletedProcess([], 0, stdout="")
    common.run_git(tmp_path, "status")
    expected = "https" if "https" in allowed.split(":") else ""
    assert run.call_args.kwargs["env"]["GIT_ALLOW_PROTOCOL"] == expected


@mock.patch("_evolution_common.subprocess.run", autospec=True)
def test_git_runner_defaults_to_safe_protocols(run: mock.Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent user allow-list retains the plugin's existing safe transports."""
    monkeypatch.delenv("GIT_ALLOW_PROTOCOL", raising=False)
    run.return_value = subprocess.CompletedProcess([], 0, stdout="")
    common.run_git(tmp_path, "status")
    assert run.call_args.kwargs["env"]["GIT_ALLOW_PROTOCOL"] == "https:ssh:git"


def test_release_index_refuses_excessive_release_count(evo_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    """Release-count bounds apply before walking history or building ancestor masks."""
    evo_repo.write("a.txt", "a")
    evo_repo.commit("base", day=0)
    evo_repo.tag("1.0.0", day=1)
    evo_repo.tag("1.1.0", day=2)
    monkeypatch.setattr(releases, "MAX_RELEASES", 1, raising=False)
    with pytest.raises(common.EvolutionError, match=r"release.*limit"):
        releases.build_release_index(evo_repo.path)
