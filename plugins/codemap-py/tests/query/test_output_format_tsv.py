"""Tests for the ``--format tsv`` result encoding.

JSON repeats every key once per row, which is the whole cost of a tabular result: measured on a 100-row ``central``
payload, 3588 tokens as JSON against 2130 as TSV. The flag is opt-in and JSON stays the default, so nothing parsing
stdout today changes.

These tests pin the two properties that make it safe rather than merely smaller: a result that is not one flat table is
refused instead of flattened, and errors stay JSON whatever format was asked for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import codemap_py.query as _query_mod  # noqa: E402  (needs the sys.path insert above)

_tabular_key = _query_mod._tabular_key
_to_tsv = _query_mod._to_tsv
_emit_tsv = _query_mod._emit_tsv


class TestTabularKey:
    """Selection of the single table inside a result payload."""

    def test_accepts_one_list_of_flat_uniform_records(self) -> None:
        """A lone list of flat dicts sharing a key order is the tabular case."""
        payload = {"central": [{"name": "a", "n": 1}, {"name": "b", "n": 2}], "index": {"stale": False}}

        assert _tabular_key(payload) == "central"

    def test_rejects_two_candidate_tables(self) -> None:
        """Two tables are ambiguous — TSV has one header, so there is no correct pick."""
        payload = {"a": [{"x": 1}], "b": [{"y": 2}]}

        assert _tabular_key(payload) is None

    def test_rejects_rows_with_differing_keys(self) -> None:
        """Ragged rows cannot share one header line without silently dropping a field."""
        payload = {"rows": [{"x": 1}, {"x": 1, "y": 2}]}

        assert _tabular_key(payload) is None

    def test_rejects_a_nested_value(self) -> None:
        """A list or dict in a cell must be refused, never stringified.

        Stringifying produces a cell no consumer can parse back, which is worse than an explicit refusal because it
        fails silently.
        """
        payload = {"rows": [{"name": "a", "tags": ["x", "y"]}]}

        assert _tabular_key(payload) is None

    def test_rejects_a_list_of_strings(self) -> None:
        """``rdeps``-style flat name lists are not tables; JSON already encodes them tightly."""
        payload = {"imported_by": ["a.b", "c.d"], "importer_count": 2}

        assert _tabular_key(payload) is None

    def test_rejects_an_empty_table(self) -> None:
        """An empty list carries no column order, so there is no header to emit."""
        assert _tabular_key({"central": []}) is None

    def test_a_lone_empty_result_is_an_empty_table_not_an_error(self) -> None:
        """A query that matched nothing is data, not a format failure.

        Refusing it would make the same command exit 0 or 1 depending on how many rows the index happens to hold that
        day.
        """
        assert _query_mod._empty_table_key({"central": [], "index": {"stale": False}}) == "central"

    def test_two_lists_are_not_an_unambiguous_empty_result(self) -> None:
        """With two lists present, an empty one is not unambiguously the result."""
        assert _query_mod._empty_table_key({"a": [], "b": [{"x": 1}]}) is None

    def test_rejects_a_zero_column_table(self) -> None:
        """Rows with no keys render as blank lines that lose the row count entirely."""
        assert _tabular_key({"rows": [{}, {}]}) is None

    def test_a_non_qualifying_sibling_list_does_not_veto_a_valid_table(self) -> None:
        """One renderable table beside an unrenderable list is still renderable.

        Vetoing on the sibling's account rejects exactly the payload shape the flag exists for — a result whose rows are
        tabular and whose metadata is not.
        """
        payload = {"rows": [{"x": 1}], "meta": [{"nested": []}]}

        assert _tabular_key(payload) == "rows"

    def test_accepts_none_valued_cells(self) -> None:
        """A null cell is scalar enough to render as an empty field."""
        payload = {"rows": [{"name": "a", "note": None}]}

        assert _tabular_key(payload) == "rows"


class TestToTsv:
    """Row rendering."""

    def test_emits_header_then_one_line_per_row(self) -> None:
        """The header names the columns once; that is where the saving comes from."""
        out = _to_tsv([{"name": "a", "n": 1}, {"name": "b", "n": 2}])

        assert out.splitlines() == ["name\tn", "a\t1", "b\t2"]

    def test_renders_none_as_an_empty_field(self) -> None:
        """A null cell must not render as the literal ``None``."""
        assert _to_tsv([{"name": "a", "note": None}]).splitlines()[1] == "a\t"

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("has\ttab", id="embedded-tab"),
            pytest.param("has\nnewline", id="embedded-newline"),
            pytest.param('has"quote', id="embedded-quote"),
        ],
    )
    def test_quotes_a_value_that_would_break_the_delimiter(self, value: str) -> None:
        """A delimiter inside a value is quoted, so a path carrying one keeps its column.

        Without quoting, one such module name shifts every later column on that row and the consumer reads wrong data
        rather than failing.
        """
        out = _to_tsv([{"name": value, "n": 1}])
        body = out.split("\n", 1)[1]

        assert body.startswith('"')
        assert body.count("\t") >= 1


class TestEmit:
    """End-to-end behaviour of the format switch."""

    @pytest.fixture(autouse=True)
    def _reset_format(self, monkeypatch) -> None:
        """Keep the module-level format global from leaking between tests."""
        monkeypatch.setattr(_query_mod, "_FORMAT", "json")
        monkeypatch.setattr(_query_mod, "_CMD", "")

    def test_json_is_the_default(self, capsys) -> None:
        """Callers that never pass --format see exactly what they saw before."""
        _query_mod._print(json.dumps({"central": [{"name": "a", "n": 1}], "index": {"stale": False}}))

        assert capsys.readouterr().out.strip() == '{"central": [{"name": "a", "n": 1}], "index": {"stale": false}}'

    def test_tsv_writes_rows_to_stdout(self, capsys, monkeypatch) -> None:
        """Rows go to stdout so the common consumer reads only the table."""
        monkeypatch.setattr(_query_mod, "_FORMAT", "tsv")

        _query_mod._print(json.dumps({"central": [{"name": "a", "n": 1}], "index": {"stale": False}}))

        assert capsys.readouterr().out.splitlines() == ["name\tn", "a\t1"]

    def test_tsv_writes_the_metadata_envelope_to_stderr(self, capsys, monkeypatch) -> None:
        """Staleness and completeness flags survive the format change.

        Dropping the envelope would make a stale or incomplete answer indistinguishable from a good one, which is the
        one thing this format must not cost.
        """
        monkeypatch.setattr(_query_mod, "_FORMAT", "tsv")

        _query_mod._print(json.dumps({"central": [{"name": "a", "n": 1}], "index": {"stale": True}}))

        assert '"stale": true' in capsys.readouterr().err

    def test_tsv_refuses_a_non_tabular_result(self, capsys, monkeypatch) -> None:
        """A non-tabular result exits non-zero with a JSON error, never a mangled table."""
        monkeypatch.setattr(_query_mod, "_FORMAT", "tsv")

        with pytest.raises(SystemExit) as excinfo:
            _query_mod._print(json.dumps({"imported_by": ["a.b"], "importer_count": 1}))

        assert excinfo.value.code != 0
        assert '"format_not_tabular"' in capsys.readouterr().out

    def test_errors_stay_json_under_tsv(self, capsys, monkeypatch) -> None:
        """``_die_json`` must not route through the formatter.

        It did once, and because an error object is not a table the formatter refused it by calling back into
        ``_die_json`` until the stack ran out.
        """
        monkeypatch.setattr(_query_mod, "_FORMAT", "tsv")

        with pytest.raises(SystemExit):
            _query_mod._die_json({"error": "boom"})

        assert capsys.readouterr().out.strip() == '{"error": "boom"}'

    def test_batch_capture_mode_keeps_json(self, capsys, monkeypatch) -> None:
        """Inside a batch the capture buffer receives JSON, whatever --format asked for.

        The batch driver owns the one real stdout write and re-parses each captured subquery as JSON; formatting there
        would leak rows past the buffer and hand the driver something it cannot read back.
        """
        monkeypatch.setattr(_query_mod, "_FORMAT", "tsv")
        buf: list[str] = []
        monkeypatch.setattr(_query_mod, "_capture", buf)

        _query_mod._print(json.dumps({"central": [{"name": "a", "n": 1}], "index": {"stale": False}}))

        assert capsys.readouterr().out == ""
        assert buf == ['{"central": [{"name": "a", "n": 1}], "index": {"stale": false}}']

    def test_empty_result_emits_no_rows_and_exits_zero(self, capsys, monkeypatch) -> None:
        """An empty table writes nothing to stdout and does not raise SystemExit."""
        monkeypatch.setattr(_query_mod, "_FORMAT", "tsv")

        _query_mod._print(json.dumps({"central": [], "index": {"stale": False}}))

        captured = capsys.readouterr()
        assert captured.out == ""
        assert '"stale": false' in captured.err
