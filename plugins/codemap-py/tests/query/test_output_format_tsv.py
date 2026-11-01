"""Tests for the ``--format tsv`` result encoding.

JSON repeats every key once per row, which is the whole cost of a tabular result: measured on a 100-row ``central``
payload, 3588 tokens as JSON against 2130 as TSV. The flag is opt-in and JSON stays the default, so nothing parsing
stdout today changes.

These tests pin the two properties that make it safe rather than merely smaller: a result that is not one flat table is
refused instead of flattened, and errors stay JSON whatever format was asked for.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import codemap_py.query as _query_mod  # noqa: E402  (needs the sys.path insert above)
from codemap_py.query.output import _pytest_command  # noqa: E402  (needs the sys.path insert above)

_LITERAL_TARGETS = ["tests/plain.py", "tests space & literal'$value/test_extra.py", r"C:\project space\test_more.py"]
_tabular_key = _query_mod._tabular_key
_to_tsv = _query_mod._to_tsv
_emit_tsv = _query_mod._emit_tsv


def _central_result(*, stale: bool = False, rows: list[dict] | None = None) -> str:
    """Return a ``central``-shaped JSON result: one flat table plus an ``index`` metadata envelope."""
    table = [{"name": "a", "n": 1}] if rows is None else rows
    return json.dumps({"central": table, "index": {"stale": stale}})


class TestTabularKey:
    """Selection of the single table inside a result payload."""

    def test_accepts_one_list_of_flat_uniform_records(self):
        """A lone list of flat dicts sharing a key order is the tabular case.

        The sibling ``index`` dict is metadata, not a table, so it must not compete with ``central``.
        """
        payload = {"central": [{"name": "a", "n": 1}, {"name": "b", "n": 2}], "index": {"stale": False}}

        assert _tabular_key(payload) == "central"

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"a": [{"x": 1}], "b": [{"y": 2}]}, id="two-candidate-tables"),
            pytest.param({"rows": [{"x": 1}, {"x": 1, "y": 2}]}, id="rows-with-differing-keys"),
            pytest.param({"rows": [{"name": "a", "tags": ["x", "y"]}]}, id="nested-value"),
            pytest.param({"imported_by": ["a.b", "c.d"], "importer_count": 2}, id="list-of-strings"),
            pytest.param({"central": []}, id="empty-table"),
            pytest.param({"rows": [{}, {}]}, id="zero-column-table"),
        ],
    )
    def test_rejects_payload_that_is_not_one_flat_table(self, payload: dict):
        """A payload with no single renderable table is refused (``None``) rather than flattened.

        Two tables are ambiguous (TSV has one header, so there is no correct pick); ragged rows cannot share one header
        line without silently dropping a field; a list or dict in a cell must be refused, never stringified, because a
        stringified cell no consumer can parse back fails silently; ``rdeps``-style flat name lists are not tables and
        JSON already encodes them tightly; an empty list carries no column order, so there is no header to emit; rows
        with no keys render as blank lines that lose the row count entirely.
        """
        assert _tabular_key(payload) is None

    def test_lone_empty_result_is_an_empty_table_not_an_error(self):
        """A query that matched nothing is data, not a format failure.

        Refusing it would make the same command exit 0 or 1 depending on how many rows the index happens to hold that
        day.
        """
        assert _query_mod._empty_table_key({"central": [], "index": {"stale": False}}) == "central"

    def test_two_lists_are_not_an_unambiguous_empty_result(self):
        """With two lists present, an empty one is not unambiguously the result.

        Picking the empty list could hide the populated one, so no key is returned.
        """
        assert _query_mod._empty_table_key({"a": [], "b": [{"x": 1}]}) is None

    def test_non_qualifying_sibling_list_does_not_veto_a_valid_table(self):
        """One renderable table beside an unrenderable list is still renderable.

        Vetoing on the sibling's account rejects exactly the payload shape the flag exists for — a result whose rows are
        tabular and whose metadata is not.
        """
        payload = {"rows": [{"x": 1}], "meta": [{"nested": []}]}

        assert _tabular_key(payload) == "rows"

    def test_accepts_none_valued_cells(self):
        """A null cell is scalar enough to render as an empty field.

        Optional attributes are routinely null in query results; refusing them would make most real tables unusable.
        """
        payload = {"rows": [{"name": "a", "note": None}]}

        assert _tabular_key(payload) == "rows"


class TestToTsv:
    """Row rendering."""

    def test_emits_header_then_one_line_per_row(self):
        """The header names the columns once; that is where the saving comes from.

        Two uniform rows render as one header line followed by one line per row, tab separated.
        """
        out = _to_tsv([{"name": "a", "n": 1}, {"name": "b", "n": 2}])

        assert out.splitlines() == ["name\tn", "a\t1", "b\t2"]

    def test_renders_none_as_an_empty_field(self):
        """A null cell must not render as the literal ``None``.

        A consumer would otherwise read the string ``None`` as real data.
        """
        assert _to_tsv([{"name": "a", "note": None}]).splitlines()[1] == "a\t"

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("has\ttab", id="embedded-tab"),
            pytest.param("has\nnewline", id="embedded-newline"),
            pytest.param('has"quote', id="embedded-quote"),
        ],
    )
    def test_quotes_a_value_that_would_break_the_delimiter(self, value: str):
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
    def _reset_format(self, monkeypatch):
        """Keep the module-level format global from leaking between tests."""
        monkeypatch.setattr(_query_mod.state, "_FORMAT", "json")
        monkeypatch.setattr(_query_mod.state, "_CMD", "")

    @pytest.fixture
    def tsv_format(self, monkeypatch):
        """Switch the module-level output format to ``tsv`` for one test."""
        monkeypatch.setattr(_query_mod.state, "_FORMAT", "tsv")

    def test_json_is_the_default(self, capsys):
        """Callers that never pass --format see exactly what they saw before.

        With the format left at its default, the result text is printed through untouched.
        """
        _query_mod._print(_central_result())

        assert capsys.readouterr().out.strip() == '{"central": [{"name": "a", "n": 1}], "index": {"stale": false}}'

    def test_tsv_writes_rows_to_stdout(self, capsys, tsv_format):
        """Rows go to stdout so the common consumer reads only the table."""
        _query_mod._print(_central_result())

        assert capsys.readouterr().out.splitlines() == ["name\tn", "a\t1"]

    def test_tsv_writes_metadata_envelope_to_stderr(self, capsys, tsv_format):
        """Staleness and completeness flags survive the format change.

        Dropping the envelope would make a stale or incomplete answer indistinguishable from a good one, which is the
        one thing this format must not cost.
        """
        _query_mod._print(_central_result(stale=True))

        assert '"stale": true' in capsys.readouterr().err

    def test_tsv_refuses_a_non_tabular_result(self, capsys, tsv_format):
        """A non-tabular result exits non-zero with a JSON error, never a mangled table.

        ``imported_by`` is a flat name list, which has no table to render, so the formatter must fail loudly.
        """
        with pytest.raises(SystemExit) as excinfo:
            _query_mod._print(json.dumps({"imported_by": ["a.b"], "importer_count": 1}))

        assert excinfo.value.code != 0
        assert '"format_not_tabular"' in capsys.readouterr().out

    def test_errors_stay_json_under_tsv(self, capsys, tsv_format):
        """``_die_json`` must not route through the formatter.

        It did once, and because an error object is not a table the formatter refused it by calling back into
        ``_die_json`` until the stack ran out.
        """
        with pytest.raises(SystemExit):
            _query_mod._die_json({"error": "boom"})

        assert capsys.readouterr().out.strip() == '{"error": "boom"}'

    def test_batch_capture_mode_keeps_json(self, capsys, monkeypatch, tsv_format):
        """Inside a batch the capture buffer receives JSON, whatever --format asked for.

        The batch driver owns the one real stdout write and re-parses each captured subquery as JSON; formatting there
        would leak rows past the buffer and hand the driver something it cannot read back.
        """
        buf: list[str] = []
        monkeypatch.setattr(_query_mod.state, "_capture", buf)

        _query_mod._print(_central_result())

        assert capsys.readouterr().out == ""
        assert buf == ['{"central": [{"name": "a", "n": 1}], "index": {"stale": false}}']

    def test_empty_result_emits_no_rows_and_exits_zero(self, capsys, tsv_format):
        """An empty table writes nothing to stdout and does not raise SystemExit.

        The metadata envelope still reaches stderr, so an empty answer remains distinguishable from a failed one.
        """
        _query_mod._print(_central_result(rows=[]))

        captured = capsys.readouterr()
        assert captured.out == ""
        assert '"stale": false' in captured.err


class TestPytestCommand:
    """``_pytest_command`` quotes each literal test path for the host shell dialect."""

    def test_posix_quoting_round_trips_through_shlex(self, monkeypatch):
        """Under a POSIX platform, spaces, apostrophes, variables and backslashes survive ``shlex.split``.

        The command is meant to be pasted into a POSIX shell, so splitting it must give back the literal file names.
        """
        monkeypatch.setattr(sys, "platform", "darwin")

        command = _pytest_command(_LITERAL_TARGETS)

        assert shlex.split(command) == ["pytest", *_LITERAL_TARGETS]

    def test_windows_quoting_uses_single_quoted_literals(self, monkeypatch):
        """Under win32, each target is a single-quoted PowerShell literal with apostrophes doubled.

        Backslashes and ``$`` stay literal inside single quotes, so the paths reach pytest untouched.
        """
        monkeypatch.setattr(sys, "platform", "win32")

        command = _pytest_command(_LITERAL_TARGETS)

        assert (
            command
            == "pytest 'tests/plain.py' 'tests space & literal''$value/test_extra.py' 'C:\\project space\\test_more.py'"
        )

    def test_empty_target_list_yields_empty_command(self):
        """No test files produce an empty command rather than a bare ``pytest`` that would run everything."""
        assert _pytest_command([]) == ""
