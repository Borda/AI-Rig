"""Tests for ``bin/filter_action_items.py``, the id-list filter behind resolve's per-item record blocks.

The script replaces two ``jq`` pipelines that printed the ``action-items.jsonl`` records named in a Write-tool id file.
Items and id files are real files under ``tmp_path``; the dispatch-contract tests run the shipped blocks end to end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import filter_action_items as fai
import pytest

_SCRIPT = Path(fai.__file__).resolve()
_MISSING_OR_EMPTY = "! BLOCKED — selected-items.txt missing or empty"
_NON_NUMERIC = "! BLOCKED — selected-items.txt holds a non-numeric item id"


def _items(tmp_path: Path, records: list[object]) -> Path:
    """Write ``records`` as an action-items file, one JSON value per line."""
    path = tmp_path / "action-items.jsonl"
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    return path


def _ids(tmp_path: Path, text: str) -> Path:
    """Write an id file with exactly ``text`` (no newline translation)."""
    path = tmp_path / "selected-items.txt"
    path.write_bytes(text.encode("utf-8"))
    return path


def _run(items: Path, ids: Path, *extra: str) -> int:
    """Run the CLI entry point in process and return its exit code."""
    return fai.main(["--items", str(items), "--ids-file", str(ids), *extra])


@pytest.mark.parametrize(
    ("records", "ids_text", "expected"),
    [
        pytest.param(
            [{"id": 1, "summary": "é"}, {"id": 2}, {"id": 3}],
            "3 1\n",
            ['{"id":1,"summary":"é"}', '{"id":3}'],
            id="item-file-order-compact-non-ascii",
        ),
        pytest.param(
            [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
            "1\r\n3\t4\n",
            ['{"id":1}', '{"id":3}', '{"id":4}'],
            id="id-file-read-across-crlf-and-tabs",
        ),
        pytest.param(
            [{"id": "7"}, {"id": 7}, {"id": 8}], "7", ['{"id":"7"}', '{"id":7}'], id="string-and-integer-ids-match"
        ),
        pytest.param([{"id": 1}], "9", [], id="unlisted-id-is-empty-selection"),
    ],
)
def test_prints_listed_records(
    tmp_path: Path, capsys: pytest.CaptureFixture, records: list[object], ids_text: str, expected: list[str]
) -> None:
    """The records named in the id file print as compact JSON lines, in item-file order, and the run succeeds.

    Scenarios in the shipped blocks' contract: ids arriving as "3 1" over a file listing 1, 2, 3 print in item order in
    the ``jq -c`` spelling (no spaces after separators, non-ASCII intact); an id file may span lines or use CRLF and
    tabs (the on-demand block used to flatten it with ``tr``, the Step 8 block read its first line); ``jq`` compared
    ``tostring`` of the id, so a hand-written string id "7" selects like the number 7; an id absent from the item file
    is an empty selection, not a failure.
    """
    code = _run(_items(tmp_path, records), _ids(tmp_path, ids_text))

    assert code == 0
    assert capsys.readouterr().out.splitlines() == expected


@pytest.mark.parametrize(
    ("content", "line"),
    [
        pytest.param("", f"{_MISSING_OR_EMPTY}; run it first", id="empty-file"),
        pytest.param(" \r\n\t", f"{_MISSING_OR_EMPTY}; run it first", id="only-separators"),
        pytest.param("abc", f"{_MISSING_OR_EMPTY}; run it first", id="no-digit-at-all"),
        pytest.param("1 x", _NON_NUMERIC, id="word-beside-digit"),
        pytest.param("1,2", _NON_NUMERIC, id="comma-separated"),
        pytest.param("1" + chr(0xA0) + "2", _NON_NUMERIC, id="no-break-space"),
    ],
)
def test_unusable_id_file_blocks_with_pinned_line(
    tmp_path: Path, capsys: pytest.CaptureFixture, content: str, line: str
) -> None:
    """An empty or non-numeric id file stops with the exact line the blocks used to print.

    A file with no digit is "missing or empty" (the hint says how to create it); any other stray character is a non-
    numeric id. Letting either through would silently select nothing or the wrong items.
    """
    code = _run(_items(tmp_path, [{"id": 1}]), _ids(tmp_path, content), "--hint", "run it first")

    assert code == 1
    assert capsys.readouterr().out.strip() == line


@pytest.mark.parametrize(
    ("extra", "line"),
    [
        pytest.param(("--hint", "run it first"), f"{_MISSING_OR_EMPTY}; run it first", id="with-hint"),
        pytest.param((), _MISSING_OR_EMPTY, id="without-hint-no-dangling-semicolon"),
    ],
)
def test_absent_id_file_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture, extra: tuple[str, ...], line: str
) -> None:
    """A missing id file stops with the "missing or empty" line, ending after "empty" when no ``--hint`` is given.

    The hint is appended after a semicolon only when supplied, so the bare line never carries a dangling ``;``.
    """
    code = _run(_items(tmp_path, [{"id": 1}]), tmp_path / "selected-items.txt", *extra)

    assert code == 1
    assert capsys.readouterr().out.strip() == line


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        pytest.param('{"id": 1}\nnot json\n', "line 2 is not valid JSON", id="malformed-line"),
        pytest.param('{"id": 1}\n[1, 2]\n', "line 2 is not a JSON object", id="non-object-line"),
    ],
)
def test_unreadable_item_file_blocks_without_partial_output(
    tmp_path: Path, capsys: pytest.CaptureFixture, body: str, fragment: str
) -> None:
    """A corrupt item line stops the run before anything prints, so a half-read list never reaches the model.

    The first line is a valid record for id 1; it must not be printed before the corrupt second line is reached.
    """
    items = tmp_path / "action-items.jsonl"
    items.write_text(body, encoding="utf-8")

    code = _run(items, _ids(tmp_path, "1"))

    out = capsys.readouterr().out
    assert code == 1
    assert out.startswith("! BLOCKED — cannot read ")
    assert fragment in out
    assert '{"id":1}' not in out


def test_missing_item_file_blocks(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """An item file that does not exist is a blocked run with the path in the line, not a traceback.

    The item file is written by an earlier resolve step; if that step failed, the caller should see which file is gone.
    """
    code = _run(tmp_path / "gone.jsonl", _ids(tmp_path, "1"))

    assert code == 1
    assert "gone.jsonl" in capsys.readouterr().out


def test_cli_output_ends_lines_with_lf_only(tmp_path: Path) -> None:
    """The installed entry point writes LF, never CRLF, so the records read the same on native Windows.

    ``jq -b`` was there for the same reason; this runs the real script as a child process.
    """
    items = _items(tmp_path, [{"id": 1}, {"id": 2}])
    ids = _ids(tmp_path, "1 2")

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--items", str(items), "--ids-file", str(ids)],
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == b'{"id":1}\n{"id":2}\n'
