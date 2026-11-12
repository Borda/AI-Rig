"""Regression coverage for bounded test selection and JSON recovery inputs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import dev_test_targets as dtt
import extract_json_field as ejf
import pytest
from dev_run_dir import write_sentinel


def test_oversized_test_file_is_not_read_as_an_importer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversized files do not contribute a truncated or unbounded import scan."""
    source = tmp_path / "test_example.py"
    source.write_text("from pkg.mod import f\n" + " " * 100, encoding="utf-8")
    monkeypatch.setattr(dtt, "_MAX_TEST_BYTES", 32, raising=False)
    corpus = dtt.SuiteFiles(tmp_path)

    assert corpus.text(source.name) == ""


def test_file_at_read_limit_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The size ceiling accepts the complete file when its byte length equals the limit."""
    source = tmp_path / "test_example.py"
    source.write_bytes(b"import pkg.mod\n")
    monkeypatch.setattr(dtt, "_MAX_TEST_BYTES", source.stat().st_size, raising=False)
    corpus = dtt.SuiteFiles(tmp_path)

    assert corpus.text(source.name) == "import pkg.mod\n"


def test_import_match_does_not_cross_source_lines(tmp_path: Path) -> None:
    """Newline-separated import fragments are not Python import statements."""
    source = tmp_path / "test_example.py"
    source.write_text("from\npkg.mod import f\n", encoding="utf-8")
    corpus = dtt.SuiteFiles(tmp_path)
    corpus.files = [source.name]

    assert dtt.heuristic_tests("pkg/mod.py", corpus) == []


@mock.patch("dev_test_targets.shutil.which", autospec=True, return_value="codemap-py")
@mock.patch("dev_test_targets.subprocess.run", autospec=True)
def test_option_shaped_module_never_reaches_provider(
    mock_run: mock.Mock, mock_which: mock.Mock, tmp_path: Path
) -> None:
    """A changed source basename beginning with a dash cannot become a provider option."""
    assert dtt.codemap_tests(dtt.module_name("--bogus.py"), tmp_path) is None
    mock_run.assert_not_called()


def test_recovery_function_enforces_its_input_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct recovery calls have the same size bound as the command-line entry point."""
    monkeypatch.setattr(ejf, "_MAX_INPUT_CHARS", 10)

    assert ejf.recover_json_object("{}" * 6) is None


@pytest.mark.parametrize(
    ("kind", "text", "code", "expected"),
    [
        pytest.param(
            "json",
            "{}" * 20_000 + '{"last":true}',
            "import json,sys; from extract_json_field import recover_json_object as r; "
            "print(json.dumps(r(open(sys.argv[1], encoding='utf-8').read())))",
            {"last": True},
            id="many-sibling-json-objects",
        ),
        pytest.param(
            "imports",
            "\n" * 50_000,
            "import json,sys; from pathlib import Path; import dev_test_targets as d; "
            "p=Path(sys.argv[1]); c=d.SuiteFiles(p.parent); c.files=[p.name]; "
            "print(json.dumps(d.heuristic_tests('pkg/mod.py', c)))",
            [],
            id="many-blank-source-lines",
        ),
    ],
)
def test_repeated_candidates_finish_within_deadline(
    tmp_path: Path, kind: str, text: str, code: str, expected: object
) -> None:
    """Repeated object starts and blank lines do not cause quadratic scans."""
    data = tmp_path / f"test_{kind}.py"
    data.write_text(text, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(dtt.__file__).parent)}

    result = subprocess.run(
        [sys.executable, "-c", code, str(data)], capture_output=True, text=True, timeout=5, check=True, env=env
    )

    assert json.loads(result.stdout) == expected


def test_record_directory_cannot_be_an_arbitrary_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A writable project directory is not sufficient authority for a run-record destination."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "sentinels"))
    monkeypatch.setenv("CSID", "selection-output-bounds")

    assert dtt.main(["--pytest-cmd", "pytest", "--record-dir", str(tmp_path)]) == 2
    assert "run directory" in json.loads(capsys.readouterr().out)["error"]


def test_failure_export_preserves_a_project_file_outside_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed run cannot export failed nodes over an unrelated project file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "sentinels"))
    monkeypatch.setenv("CSID", "selection-output-bounds")
    log = tmp_path / "failure.log"
    log.write_text("FAILED tests/test_example.py::test_example - AssertionError\n", encoding="utf-8")
    record = tmp_path / "completed.jsonl"
    record.write_text(
        json.dumps({"tests": ["tests/test_example.py"], "exit": 1, "log": str(log)}) + "\n", encoding="utf-8"
    )
    destination = tmp_path / "project.json"
    destination.write_text("user content", encoding="utf-8")

    assert dtt.main(["--check-record", str(record), "--expect-exit", "1", "--save-failures", str(destination)]) == 2
    assert destination.read_text(encoding="utf-8") == "user content"


def test_selection_record_uses_the_active_run_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The persisted skill directory permits its own selection ledger."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "sentinels"))
    monkeypatch.setenv("CSID", "selection-output-bounds")
    (tmp_path / "sentinels").mkdir()
    run = tmp_path / "run"
    write_sentinel("dev-fix-dev-dir", str(run))

    dtt.record(run, {"tests": []}, None, None)

    assert json.loads((run / "test-targets.jsonl").read_text(encoding="utf-8"))["tests"] == []
