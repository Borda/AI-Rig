"""Tests for ``bin/extract_json_field.py``.

Verifies JSON object recovery from mixed-prose text and field extraction (happy path, whole-object aliases, absent
field, no JSON, stdin, usage error).
"""

from __future__ import annotations

import json
import sys

import pytest

import extract_json_field  # type: ignore[import-not-found]
from extract_json_field import format_field, recover_json_object


# ---------------------------------------------------------------------------
# recover_json_object
# ---------------------------------------------------------------------------


class TestRecoverJsonObject:
    """Unit tests for :func:`recover_json_object`."""

    def test_plain_json(self) -> None:
        """Extracts a plain JSON object with no surrounding prose."""
        result = recover_json_object('{"status":"done","files_changed":1}')
        assert result == {"status": "done", "files_changed": 1}

    def test_prose_preamble(self) -> None:
        """Extracts JSON object preceded by prose."""
        result = recover_json_object('thinking... here it is: {"verdict":"PASS"}')
        assert result == {"verdict": "PASS"}

    def test_stray_brace_before_json(self) -> None:
        """Prefers rightmost / outermost valid JSON when stray braces precede it."""
        result = recover_json_object('prose with { stray brace and {"ok":false}')
        assert result == {"ok": False}

    def test_trailing_prose(self) -> None:
        """Extracts JSON object followed by trailing prose."""
        result = recover_json_object('  {"ok": true}\n\nthen extra prose')
        assert result == {"ok": True}

    def test_nested_object(self) -> None:
        """Return outermost object when nested objects are present."""
        result = recover_json_object('{"nested":{"k":1}} trailing')
        assert result == {"nested": {"k": 1}}

    @pytest.mark.parametrize(
        "text,expected",
        [
            pytest.param(
                'prefix {"message":"literal { brace }"} suffix',
                {"message": "literal { brace }"},
                id="prefix-message-literal-brace-suffix",
            ),
            pytest.param(
                'bad {"broken": true trailing {"ok": true}', {"ok": True}, id="bad-broken-true-trailing-ok-true"
            ),
            pytest.param('first {"a":1} second {"b":[{"c":2}]}', {"b": [{"c": 2}]}, id="first-a-1-second-b-c-2"),
        ],
    )
    def test_brace_heavy_recovery(self, text: str, expected: dict[str, object]) -> None:
        """Recovery handles braces in strings, invalid leading objects, and nested arrays."""
        assert recover_json_object(text) == expected

    def test_no_json_returns_none(self) -> None:
        """Return None when no JSON object is present."""
        assert recover_json_object("no json here at all") is None

    def test_empty_string_returns_none(self) -> None:
        """Return None for empty input."""
        assert recover_json_object("") is None

    @pytest.mark.parametrize("alias", [".", "_object", ""])
    def test_whole_object_aliases_recognized(self, alias: str) -> None:
        """Whole-object aliases (., _object, '') are in the frozenset."""
        assert alias in extract_json_field._WHOLE_OBJECT_ALIASES


# ---------------------------------------------------------------------------
# format_field
# ---------------------------------------------------------------------------


class TestFormatField:
    """Unit tests for :func:`format_field`."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            pytest.param("PASS", "PASS", id="pass"),
            pytest.param(True, "true", id="true"),
            pytest.param(False, "false", id="false"),
            pytest.param(42, "42", id="42"),
            pytest.param([1, 2, 3], "[1, 2, 3]", id="1-2-3"),
            pytest.param({"k": "v"}, '{"k": "v"}', id="k-v"),
            pytest.param(None, "null", id="none"),
        ],
    )
    def test_format(self, value: object, expected: str) -> None:
        """Formats each JSON type correctly for stdout."""
        assert format_field(value) == expected  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# main() — CLI behaviour
# ---------------------------------------------------------------------------


class TestMain:
    """Integration tests for :func:`main` (CLI entry point)."""

    def test_extract_string_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Extract and print a string field without JSON quotes."""
        rc = extract_json_field.main(["status", '{"status":"done","n":3}'])
        out, _ = capsys.readouterr()
        assert rc == 0
        assert out.strip() == "done"

    def test_extract_int_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Extract and print an integer field as JSON."""
        rc = extract_json_field.main(["files_changed", '{"status":"ok","files_changed":2}'])
        out, _ = capsys.readouterr()
        assert rc == 0
        assert out.strip() == "2"

    def test_extract_bool_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Extracts a boolean field as lowercase JSON."""
        rc = extract_json_field.main(["re_audit_clean", '{"re_audit_clean":true}'])
        out, _ = capsys.readouterr()
        assert rc == 0
        assert out.strip() == "true"

    @pytest.mark.parametrize("alias", [".", "_object"])
    def test_whole_object_alias(self, alias: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Whole-object aliases print compact JSON of the full recovered object."""
        rc = extract_json_field.main([alias, '{"a":1,"b":2}'])
        out, _ = capsys.readouterr()
        assert rc == 0
        parsed = json.loads(out.strip())
        assert parsed == {"a": 1, "b": 2}

    def test_field_absent_returns_exit_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Return exit code 2 when the requested field is not in the object."""
        rc = extract_json_field.main(["missing", '{"other":"val"}'])
        assert rc == 2

    def test_no_json_returns_exit_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Return exit code 1 when no JSON object is recoverable."""
        rc = extract_json_field.main(["field", "just plain text"])
        assert rc == 1

    def test_no_args_returns_exit_3(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Return exit code 3 (usage error) when no arguments provided."""
        rc = extract_json_field.main([])
        assert rc == 3

    @pytest.mark.parametrize("argv", [["verdict", "-"], ["verdict"]])
    def test_stdin_input(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
    ) -> None:
        """Reads JSON from stdin when second arg is '-' or omitted."""
        import io

        monkeypatch.setattr(sys, "stdin", io.StringIO('{"verdict":"approved"}'))
        rc = extract_json_field.main(argv)
        out, _ = capsys.readouterr()
        assert rc == 0
        assert out.strip() == "approved"

    def test_prose_wrapped_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Recovers JSON embedded in reasoning/prose output from agents."""
        prose = 'I reviewed the findings. Here is my response: {"status":"done","fixed":5} Thank you.'
        rc = extract_json_field.main(["fixed", prose])
        out, _ = capsys.readouterr()
        assert rc == 0
        assert out.strip() == "5"

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage to stdout and exit 0 (argparse default)."""
        with pytest.raises(SystemExit) as exc:
            extract_json_field.main(["--help"])
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()

    def test_dash_leading_text_blob_handled_opaquely(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A ``<json-or-text>`` blob beginning with ``--`` is captured, not parsed as an option.

        argparse would reject a ``--``-leading second positional as an unknown option (exit 2). The script hands
        positionals through directly, so the recovery scan still finds the embedded object and prints the field.
        """
        rc = extract_json_field.main(["ok", '--flag noise {"ok":true} trailing'])
        out, _ = capsys.readouterr()
        assert rc == 0
        assert out.strip() == "true"
