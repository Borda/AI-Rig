"""Exercise bounds and trust checks at codemap helper input boundaries."""

from __future__ import annotations

import io
import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import anonymize
import check_index_smoke
import cm_json
import debrief_stats
import find_module_refs
import gen_deprecation_wrapper
import join_avoidance
import locate_scan_query
import parse_scan_args
import pytest
import resolve_index_env
import smoke_test_index
from _host_skips import skip_file_symlink_unavailable


def test_windows_launcher_disables_implicit_cwd_lookup() -> None:
    """The Windows resolver disables implicit cwd lookup before any interpreter probe."""
    launcher = Path(anonymize.__file__).parent / "codemap-py.cmd"
    source = launcher.read_text(encoding="utf-8")
    assert source.index('set "NoDefaultCurrentDirectoryInExePath=1"') < source.index("py -%%V -c")


@pytest.mark.parametrize(
    ("relative", "allowed"),
    [
        pytest.param(".claude/index.json", True, id="claude-cache"),
        pytest.param("private.json", False, id="private-home"),
    ],
)
def test_smoke_wrappers_share_index_policy(
    relative: str, allowed: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outer smoke wrapper cannot accept private home files rejected by the inner helper."""
    home, cwd, tempdir = tmp_path / "home", tmp_path / "cwd", tmp_path / "temp"
    home.mkdir()
    cwd.mkdir()
    tempdir.mkdir()
    candidate = home / relative
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(cwd)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tempdir))
    assert check_index_smoke._validate_index_path(str(candidate))[0] is allowed
    assert (smoke_test_index._validate_index_path(str(candidate)) is not None) is allowed


@pytest.mark.parametrize("field", ["error", "stderr"])
def test_diagnostic_hides_paths_and_bare_names(field: str) -> None:
    """Diagnostics cannot expose plaintext counterparts to pseudonyms in argv."""
    raw = "/Users/privateuser/privateproject/private_symbol.py private_symbol"
    record = {"argv": [raw], "result": {field: raw}}
    result = anonymize.anonymize_record(record, b"x" * 32)
    assert result["result"][field] == result["argv"][0]
    assert "privateuser" not in json.dumps(result)
    assert "privateproject" not in json.dumps(result)
    assert "private_symbol" not in json.dumps(result)


def test_locator_rejects_foreign_absolute_plugin_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absolute environment path cannot nominate a foreign executable."""
    exe = tmp_path / "foreign" / "bin" / "scan-query"
    exe.parent.mkdir(parents=True)
    exe.write_text("fake", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(exe.parent.parent))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(locate_scan_query.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError):
        locate_scan_query.locate_scan_query()


@pytest.mark.parametrize("kind", ["sphinx-symbol", "sphinx-mod"])
def test_sphinx_roles_cannot_span_lines(kind: str, tmp_path: Path) -> None:
    """An unclosed role cannot search subsequent lines for a closing backtick."""
    source = tmp_path / "api.rst"
    source.write_text(":mod:`unclosed\npkg.old old_name`\n", encoding="utf-8")
    hits, _ = find_module_refs.find_refs(kind, "pkg.old", "old_name", str(tmp_path))
    assert hits == []
    pattern = find_module_refs._KINDS[kind].patterns("pkg.old", "old_name")[0]
    assert pattern.search(source.read_text(encoding="utf-8")) is None


@pytest.mark.parametrize("kind", ["sphinx-symbol", "sphinx-mod"])
def test_unclosed_sphinx_role_scan_is_bounded(kind: str) -> None:
    """A long unclosed role with repeated target names must not backtrack quadratically."""
    text = ":mod:`" + "pkg.old old_name " * 16_000
    pattern = find_module_refs._KINDS[kind].patterns("pkg.old", "old_name")[0]
    started = time.monotonic()
    assert pattern.search(text) is None
    assert time.monotonic() - started < 3


def test_search_refuses_oversized_required_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A required file above the scan ceiling fails rather than claiming no references."""
    source = tmp_path / "large.py"
    source.write_text("old_name" * 4, encoding="utf-8")
    monkeypatch.setattr(find_module_refs, "MAX_FILE_SIZE", 16, raising=False)
    pattern = find_module_refs._name("", "old_name")[0]
    with pytest.raises(OSError, match="too large"):
        list(find_module_refs.search_file(str(source), pattern, required=True))


def test_incomplete_batch_child_rejected() -> None:
    """A successful but incomplete rdeps child cannot narrow the reference edit set."""
    batch = [
        {"cmd": "list", "ok": True, "result": {"shown": 0, "total": 0, "modules": []}},
        {"cmd": "rdeps", "ok": True, "result": {"module": "p", "index": {"query_complete": False}}},
    ]
    with pytest.raises(ValueError, match="incomplete"):
        find_module_refs.answer_paths({"batch": batch})


@pytest.mark.parametrize("relative", [False, True])
def test_saved_answer_cannot_read_outside_scope(
    relative: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Absolute and parent paths in saved answers cannot export an outside file."""
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("private_symbol secret", encoding="utf-8")
    path = "../secret.txt" if relative else str(tmp_path / "secret.txt")
    answer = root / "answer.json"
    answer.write_text(json.dumps({"matches": [{"path": path}]}), encoding="utf-8")
    monkeypatch.chdir(root)
    assert find_module_refs.main(["--kind", "name", "--name", "private_symbol", "--files-from-json", str(answer)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "outside" in captured.err


@pytest.mark.parametrize(
    "decorator",
    [
        "@deprecated(**kw)",
        "@deprecated(target=\uff50\uff52\uff49\uff4e\uff54)",
        "@deprecated(\uff54\uff41\uff52\uff47\uff45\uff54=target)",
    ],
)
def test_decorator_rejects_unpacking_and_non_ascii_identifiers(decorator: str) -> None:
    """Explicit decorators cannot smuggle unpacking or normalized identifier names."""
    with pytest.raises(ValueError, match="--decorator"):
        gen_deprecation_wrapper._decorator_func_name(decorator)


@pytest.mark.parametrize("stdin", [False, True])
def test_json_input_limit(stdin: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """File and standard-input JSON reads reject content beyond their ceiling."""
    raw = json.dumps({"x": "a" * 30})
    source = tmp_path / "answer.json"
    source.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(cm_json, "MAX_INPUT_SIZE", 16, raising=False)
    monkeypatch.setattr(cm_json.sys, "stdin", io.StringIO(raw))
    with pytest.raises(ValueError, match="too large"):
        cm_json._load_json("-" if stdin else str(source))


def test_join_replaces_invalid_utf8(tmp_path: Path) -> None:
    """A malformed UTF-8 string cannot crash a whole telemetry join."""
    source = tmp_path / "cli.jsonl"
    source.write_bytes(b'{"cmd":"rdeps","note":"\xff"}\n')
    assert join_avoidance._read_jsonl(source) == [{"cmd": "rdeps", "note": "\ufffd"}]


def test_join_caps_aggregate_shards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Individually small shards cannot bypass the aggregate input ceiling."""
    first, second = tmp_path / "cli_a.jsonl", tmp_path / "cli_b.jsonl"
    first.write_text('{"x":1}\n', encoding="utf-8")
    second.write_text('{"x":2}\n', encoding="utf-8")
    monkeypatch.setattr(join_avoidance, "MAX_TOTAL_LOG_SIZE", 12, raising=False)
    with pytest.raises(ValueError, match="aggregate"):
        join_avoidance._collect([first, second])


def test_join_many_modules_stays_within_helper_budget() -> None:
    """Distinct modules in one session must not repeatedly compile per answer/event pair."""
    now = datetime.now(timezone.utc)
    answers = [join_avoidance.CliAnswer("session", now, f"package.module_{i}") for i in range(1000)]
    events = [join_avoidance.ToolEvent("session", now, "Read", f"package/module_{i}.py") for i in range(1000)]
    started = time.monotonic()
    result = join_avoidance.find_avoidance_events(answers, events)
    elapsed = time.monotonic() - started
    assert len(result) == 1000
    assert {item.module for item in result} == {item.module for item in answers}
    assert elapsed < 8, f"join took {elapsed:.2f}s"


def test_join_ignores_empty_module() -> None:
    """An empty answer module cannot match arbitrary tool targets."""
    now = datetime.now(timezone.utc)
    answer = join_avoidance.CliAnswer("session", now, "")
    event = join_avoidance.ToolEvent("session", now, "Read", "package//module.py")
    assert join_avoidance.find_avoidance_events([answer], [event]) == []


def test_explicit_report_path_preserves_existing_file(tmp_path: Path) -> None:
    """An explicit output inside the project receives a free suffixed filename."""
    target = tmp_path / "report.md"
    target.write_text("keep", encoding="utf-8")
    options = debrief_stats.DebriefOptions(output=str(target))
    assert debrief_stats.report_path(tmp_path, options, date(2026, 1, 1)) == tmp_path / "report-2.md"
    assert target.read_text(encoding="utf-8") == "keep"


def test_report_path_cannot_escape_project(tmp_path: Path) -> None:
    """Explicit output paths outside the project are rejected before directory creation."""
    options = debrief_stats.DebriefOptions(output=str(tmp_path.parent / "outside.md"))
    with pytest.raises(debrief_stats.OptionsError, match="outside"):
        debrief_stats.report_path(tmp_path, options, date(2026, 1, 1))


def test_debrief_caps_loaded_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aggregating many small records cannot exceed the in-memory record ceiling."""
    (tmp_path / "cli_a.jsonl").write_text('{"cmd":"rdeps"}\n' * 3, encoding="utf-8")
    monkeypatch.setattr(debrief_stats, "MAX_RECORDS", 2, raising=False)
    with pytest.raises(debrief_stats.OptionsError, match="record limit"):
        debrief_stats.load_dataset(tmp_path)


def test_debrief_staging_caps_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Staging applies a record ceiling before building the filtered copy."""
    (tmp_path / "cli_a.jsonl").write_text('{"cmd":"rdeps"}\n' * 3, encoding="utf-8")
    monkeypatch.setattr(debrief_stats, "MAX_RECORDS", 2, raising=False)
    with pytest.raises(debrief_stats.OptionsError, match="record limit"):
        debrief_stats.stage_filtered_copy(tmp_path, tmp_path / "staged", debrief_stats.DebriefOptions())


def test_debrief_caps_single_record_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single large record cannot bypass the record-count ceiling."""
    (tmp_path / "cli_a.jsonl").write_text('{"cmd":"' + "x" * 30 + '"}\n', encoding="utf-8")
    monkeypatch.setattr(debrief_stats, "MAX_DATASET_BYTES", 16)
    with pytest.raises(debrief_stats.OptionsError, match="byte limit"):
        debrief_stats.load_dataset(tmp_path)


@skip_file_symlink_unavailable
def test_debrief_skips_symlinked_shards(tmp_path: Path) -> None:
    """A final-component shard symlink is excluded from staging and aggregation."""
    source = tmp_path / "real.jsonl"
    source.write_text('{"cmd":"rdeps"}\n', encoding="utf-8")
    (tmp_path / "cli_link.jsonl").symlink_to(source)
    assert debrief_stats.load_dataset(tmp_path)["cli"] == []
    report = debrief_stats.stage_filtered_copy(tmp_path, tmp_path / "staged", debrief_stats.DebriefOptions())
    assert report.kept["cli"] == 0


@skip_file_symlink_unavailable
def test_debrief_skips_symlinked_runtime_directory(tmp_path: Path) -> None:
    """Runtime labels cannot follow directory links even when the destination stays under the log root."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "cli_real.jsonl").write_text('{"cmd":"rdeps"}\n', encoding="utf-8")
    (tmp_path / "claude").symlink_to(source, target_is_directory=True)
    assert debrief_stats.load_dataset(tmp_path)["cli"] == []
    report = debrief_stats.stage_filtered_copy(tmp_path, tmp_path / "staged", debrief_stats.DebriefOptions())
    assert report.kept["cli"] == 0


def test_nul_output_checks_owner_before_truncate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign-owned writable sentinel is refused with its original bytes intact."""
    source = tmp_path / "args.nul"
    source.write_bytes(b"keep")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(resolve_index_env, "_resolve_tmpdir", lambda: str(tmp_path))
    real_fstat = os.fstat
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "fstat", lambda fd: mock.Mock(st_uid=1001, st_mode=real_fstat(fd).st_mode))
    assert parse_scan_args.main(["--incremental", "--nul-output", str(source)]) == 1
    assert source.read_bytes() == b"keep"
