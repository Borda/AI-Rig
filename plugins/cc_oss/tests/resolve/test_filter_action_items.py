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


def test_prints_listed_records_in_item_order_as_compact_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Records print in item-file order, not id-file order, compact and with non-ASCII text intact.

    The ids arrive as "3 1" while the file lists 1, 2, 3: the shipped blocks promise one record per id in the item
    file's order, the way ``jq`` streamed it, and the ``jq -c`` spelling without spaces after separators.
    """
    items = _items(tmp_path, [{"id": 1, "summary": "é"}, {"id": 2}, {"id": 3}])

    code = _run(items, _ids(tmp_path, "3 1\n"))

    assert code == 0
    assert capsys.readouterr().out.splitlines() == ['{"id":1,"summary":"é"}', '{"id":3}']


def test_id_file_is_read_whole_across_line_breaks_and_tabs(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A Write-tool id file may list ids on several lines or with CRLF; every one is selected.

    The on-demand block used to flatten the whole file with ``tr``, the Step 8 block read its first line; both are one
    contract now, and ``selected-items.txt`` is always a single line, so the wider read changes nothing for it.
    """
    items = _items(tmp_path, [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}])

    code = _run(items, _ids(tmp_path, "1\r\n3\t4\n"))

    assert code == 0
    assert capsys.readouterr().out.splitlines() == ['{"id":1}', '{"id":3}', '{"id":4}']


def test_string_and_integer_ids_both_match_their_text(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """An item whose id is the string "7" and one whose id is the number 7 are both selected by ``7``.

    ``jq`` compared ``tostring`` of the id with the listed text, so a hand-written string id still selects.
    """
    items = _items(tmp_path, [{"id": "7"}, {"id": 7}, {"id": 8}])

    code = _run(items, _ids(tmp_path, "7"))

    assert code == 0
    assert capsys.readouterr().out.splitlines() == ['{"id":"7"}', '{"id":7}']


def test_unlisted_ids_print_nothing_and_succeed(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """No listed id in the item file is an empty selection, not a failure."""
    code = _run(_items(tmp_path, [{"id": 1}]), _ids(tmp_path, "9"))

    assert code == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("content", "line"),
    [
        pytest.param(None, "! BLOCKED — selected-items.txt missing or empty; run it first", id="missing-file"),
        pytest.param("", "! BLOCKED — selected-items.txt missing or empty; run it first", id="empty-file"),
        pytest.param(" \r\n\t", "! BLOCKED — selected-items.txt missing or empty; run it first", id="only-separators"),
        pytest.param("abc", "! BLOCKED — selected-items.txt missing or empty; run it first", id="no-digit-at-all"),
        pytest.param("1 x", "! BLOCKED — selected-items.txt holds a non-numeric item id", id="word-beside-digit"),
        pytest.param("1,2", "! BLOCKED — selected-items.txt holds a non-numeric item id", id="comma-separated"),
        pytest.param("1\u00a02", "! BLOCKED — selected-items.txt holds a non-numeric item id", id="no-break-space"),
    ],
)
def test_unusable_id_file_blocks_with_the_pinned_line(
    tmp_path: Path, capsys: pytest.CaptureFixture, content: str | None, line: str
) -> None:
    """An absent, empty or non-numeric id file stops with the exact line the blocks used to print.

    A file with no digit is "missing or empty" (the hint says how to create it); any other stray character is a non-
    numeric id. Letting either through would silently select nothing or the wrong items.
    """
    items = _items(tmp_path, [{"id": 1}])
    ids = tmp_path / "selected-items.txt"
    if content is not None:
        ids.write_bytes(content.encode("utf-8"))

    code = _run(items, ids, "--hint", "run it first")

    assert code == 1
    assert capsys.readouterr().out.strip() == line


def test_missing_or_empty_line_has_no_dangling_semicolon_without_a_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Without ``--hint`` the missing-or-empty line ends after "empty"."""
    code = _run(_items(tmp_path, [{"id": 1}]), tmp_path / "absent.txt")

    assert code == 1
    assert capsys.readouterr().out.strip() == "! BLOCKED — absent.txt missing or empty"


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
    """A corrupt item line stops the run before anything prints, so a half-read list never reaches the model."""
    items = tmp_path / "action-items.jsonl"
    items.write_text(body, encoding="utf-8")

    code = _run(items, _ids(tmp_path, "1"))

    assert code == 1
    out = capsys.readouterr().out
    assert out.startswith("! BLOCKED — cannot read ")
    assert fragment in out
    assert '{"id":1}' not in out


def test_missing_item_file_blocks(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """An item file that does not exist is a blocked run with the path in the line, not a traceback."""
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
