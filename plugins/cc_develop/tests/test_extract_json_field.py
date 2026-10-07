"""Tests for ``bin/extract_json_field.py``.

Verifies JSON object recovery from mixed-prose text and field extraction (happy path, whole-object aliases, absent
field, no JSON, stdin, usage error).
"""

from __future__ import annotations

import json
import sys

import extract_json_field  # type: ignore[import-not-found]
import pytest
from extract_json_field import format_field, recover_json_object

# ---------------------------------------------------------------------------
# recover_json_object
# ---------------------------------------------------------------------------


class TestRecoverJsonObject:
    """Unit tests for :func:`recover_json_object`."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                '{"status":"done","files_changed":1}', {"status": "done", "files_changed": 1}, id="plain-json"
            ),
            pytest.param('thinking... here it is: {"verdict":"PASS"}', {"verdict": "PASS"}, id="prose-preamble"),
            pytest.param(
                'prose with { stray brace and {"ok":false}',
                {"ok": False},
                id="stray-brace-before-json-prefers-rightmost",
            ),
            pytest.param('  {"ok": true}\n\nthen extra prose', {"ok": True}, id="trailing-prose"),
            pytest.param('{"nested":{"k":1}} trailing', {"nested": {"k": 1}}, id="nested-object-outermost"),
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
    def test_recovers_json_object_from_mixed_text(self, text: str, expected: dict[str, object]) -> None:
        """The JSON object is recovered from plain JSON and from JSON mixed with prose.

        Covers surrounding prose before and after the object, a stray brace before it (the rightmost valid object wins),
        the outermost object when objects are nested, braces inside strings, an invalid leading object, and nested
        arrays.
        """
        assert recover_json_object(text) == expected

    @pytest.mark.parametrize(
        "text",
        [pytest.param("no json here at all", id="no-json-present"), pytest.param("", id="empty-input")],
    )
    def test_no_json_object_returns_none(self, text: str) -> None:
        """Return None when no JSON object is present, including for empty input."""
        assert recover_json_object(text) is None

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
        ("value", "expected"),
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

    @pytest.mark.parametrize(
        ("field", "text", "expected"),
        [
            pytest.param("status", '{"status":"done","n":3}', "done", id="string-without-json-quotes"),
            pytest.param("files_changed", '{"status":"ok","files_changed":2}', "2", id="int-as-json"),
            pytest.param("re_audit_clean", '{"re_audit_clean":true}', "true", id="bool-as-lowercase-json"),
            pytest.param(
                "fixed",
                'I reviewed the findings. Here is my response: {"status":"done","fixed":5} Thank you.',
                "5",
                id="json-embedded-in-agent-prose",
            ),
            pytest.param("ok", '--flag noise {"ok":true} trailing', "true", id="dash-leading-blob-handled-opaquely"),
        ],
    )
    def test_extract_field(self, field: str, text: str, expected: str, capsys: pytest.CaptureFixture[str]) -> None:
        """The requested field is printed on stdout in its formatted form and the exit code is 0.

        Strings print without JSON quotes, integers as JSON and booleans as lowercase JSON. JSON embedded in agent
        reasoning/prose is recovered. A ``<json-or-text>`` blob beginning with ``--`` is captured, not parsed as an
        option: argparse would reject a ``--``-leading second positional as an unknown option (exit 2), but the script
        hands positionals through directly, so the recovery scan still finds the embedded object.
        """
        rc = extract_json_field.main([field, text])
        out, _ = capsys.readouterr()
        assert rc == 0
        assert out.strip() == expected

    @pytest.mark.parametrize("alias", [".", "_object"])
    def test_whole_object_alias(self, alias: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Whole-object aliases print compact JSON of the full recovered object."""
        rc = extract_json_field.main([alias, '{"a":1,"b":2}'])
        out, _ = capsys.readouterr()
        assert rc == 0
        parsed = json.loads(out.strip())
        assert parsed == {"a": 1, "b": 2}

    @pytest.mark.parametrize(
        ("argv", "expected_rc"),
        [
            pytest.param(["missing", '{"other":"val"}'], 2, id="field-absent-exit-2"),
            pytest.param(["field", "just plain text"], 1, id="no-json-exit-1"),
            pytest.param([], 3, id="no-args-usage-error-exit-3"),
        ],
    )
    def test_failure_exit_codes(self, argv: list[str], expected_rc: int, capsys: pytest.CaptureFixture[str]) -> None:
        """Return exit code 2 when the field is absent, 1 when no JSON is recoverable, 3 (usage error) with no
        arguments."""
        rc = extract_json_field.main(argv)
        assert rc == expected_rc

    @pytest.mark.parametrize(
        "argv", [pytest.param(["verdict", "-"], id="explicit-stdin"), pytest.param(["verdict"], id="implicit-stdin")]
    )
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

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage to stdout and exit 0 (argparse default)."""
        with pytest.raises(SystemExit) as exc:
            extract_json_field.main(["--help"])
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()
