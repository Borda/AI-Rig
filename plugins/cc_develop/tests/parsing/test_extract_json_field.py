"""Tests for ``bin/extract_json_field.py``.

Verifies JSON object recovery from mixed-prose text and field extraction (happy path, whole-object aliases, absent
field, no JSON, stdin, usage error).
"""

from __future__ import annotations

import io
import json
import sys

import extract_json_field  # type: ignore[import-not-found]
import pytest
from extract_json_field import format_field, recover_json_object


class TestRecoveryIsBounded:
    """Recovery stays linear on large objects and gives up on pathological input instead of stalling the caller."""

    def test_large_object_is_recovered(self) -> None:
        """Twenty thousand nested objects parse in one pass.

        The earlier scan retried every ``{`` against every trailing ``}``, so a big reply made the caller wait minutes.
        """
        payload = {"items": [{"i": i} for i in range(20_000)]}

        assert recover_json_object("result: " + json.dumps(payload) + " done") == payload

    def test_unclosed_deep_nesting_gives_up(self) -> None:
        """An unclosed, deeply nested run of braces yields ``None`` rather than a ``RecursionError`` or a long scan.

        Three times the failed-start cap is both past the cap and deeper than the interpreter recursion limit, so the
        give-up path runs without scanning a hundred-thousand-brace input end to end.
        """
        opened = '{"a":' * (extract_json_field._MAX_FAILED_STARTS * 3)

        assert recover_json_object(opened) is None

    def test_input_over_the_cap_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Stdin longer than the cap exits 1 with a diagnostic; the cap is read from one character past it.

        The cap is lowered to ten characters so the oversize path needs no megabyte payload.
        """
        monkeypatch.setattr(extract_json_field, "_MAX_INPUT_CHARS", 10)
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"a":1}' + " " * 4))

        assert (extract_json_field.main(["a", "-"]), "exceeds" in capsys.readouterr().err) == (1, True)

    def test_input_at_the_cap_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Input exactly at the cap still parses.

        Together with the over-the-cap case this pins the boundary at ``cap`` characters, not ``cap - 1``.
        """
        monkeypatch.setattr(extract_json_field, "_MAX_INPUT_CHARS", 7)
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"a":1}'))

        assert (extract_json_field.main(["a", "-"]), capsys.readouterr().out) == (0, "1\n")


class TestRecoverJsonObject:
    """The rightmost top-level JSON object is recovered from text that may carry prose around it."""

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
                'prefix {"message":"literal { brace }"} suffix', {"message": "literal { brace }"}, id="brace-in-string"
            ),
            pytest.param('bad {"broken": true trailing {"ok": true}', {"ok": True}, id="invalid-leading-object"),
            pytest.param('first {"a":1} second {"b":[{"c":2}]}', {"b": [{"c": 2}]}, id="rightmost-sibling-with-array"),
        ],
    )
    def test_recovers_object_from_mixed_text(self, text: str, expected: dict[str, object]) -> None:
        """The JSON object is recovered from plain JSON and from JSON mixed with prose.

        Covers surrounding prose before and after the object, a stray brace before it (the rightmost valid object wins),
        the outermost object when objects are nested, braces inside strings, an invalid leading object, and nested
        arrays.
        """
        assert recover_json_object(text) == expected

    @pytest.mark.parametrize(
        "text", [pytest.param("no json here at all", id="no-json-present"), pytest.param("", id="empty-input")]
    )
    def test_no_object_returns_none(self, text: str) -> None:
        """Return None when no JSON object is present, including for empty input.

        ``None`` (not an exception) is what lets the caller print its own "no JSON" diagnostic and exit 1.
        """
        assert recover_json_object(text) is None

    @pytest.mark.parametrize("alias", [".", "_object", ""])
    def test_whole_object_alias_is_registered(self, alias: str) -> None:
        """Whole-object aliases (``.``, ``_object``, empty) are in the alias set the CLI consults.

        The CLI treats these field names as "print the full object"; dropping one silently turns it into a lookup.
        """
        assert alias in extract_json_field._WHOLE_OBJECT_ALIASES


class TestFormatField:
    """Each JSON value type prints in the form shell callers expect."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("PASS", "PASS", id="string-unquoted"),
            pytest.param(True, "true", id="true"),
            pytest.param(False, "false", id="false"),
            pytest.param(42, "42", id="int"),
            pytest.param([1, 2, 3], "[1, 2, 3]", id="list"),
            pytest.param({"k": "v"}, '{"k": "v"}', id="dict"),
            pytest.param(None, "null", id="none"),
        ],
    )
    def test_formats_json_types(self, value: object, expected: str) -> None:
        """Strings print raw; every other JSON type prints as JSON (lowercase booleans, ``null``)."""
        assert format_field(value) == expected  # type: ignore[arg-type]


class TestMain:
    """CLI entry point: field extraction, whole-object aliases, stdin and failure exit codes."""

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
        assert (rc, capsys.readouterr().out.strip()) == (0, expected)

    @pytest.mark.parametrize("alias", [".", "_object"])
    def test_whole_object_alias_prints_compact_json(self, alias: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Whole-object aliases print compact JSON of the full recovered object.

        Parsing the output (rather than comparing text) keeps the check independent of key order and spacing.
        """
        rc = extract_json_field.main([alias, '{"a":1,"b":2}'])
        assert (rc, json.loads(capsys.readouterr().out.strip())) == (0, {"a": 1, "b": 2})

    @pytest.mark.parametrize(
        ("argv", "expected_rc"),
        [
            pytest.param(["missing", '{"other":"val"}'], 2, id="field-absent-exit-2"),
            pytest.param(["field", "just plain text"], 1, id="no-json-exit-1"),
            pytest.param([], 3, id="no-args-usage-error-exit-3"),
        ],
    )
    def test_failure_exit_codes(self, argv: list[str], expected_rc: int) -> None:
        """Exit 2 when the field is absent, 1 when no JSON is recoverable, 3 (usage error) with no arguments.

        Distinct codes let shell callers tell "agent answered without the field" from "agent returned no JSON".
        """
        assert extract_json_field.main(argv) == expected_rc

    @pytest.mark.parametrize(
        "argv", [pytest.param(["verdict", "-"], id="explicit-stdin"), pytest.param(["verdict"], id="implicit-stdin")]
    )
    def test_reads_stdin(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, argv: list[str]
    ) -> None:
        """Reads JSON from stdin when the second argument is ``-`` or omitted.

        Callers pipe a long agent reply through stdin to stay under the argv length limit.
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"verdict":"approved"}'))
        rc = extract_json_field.main(argv)
        assert (rc, capsys.readouterr().out.strip()) == (0, "approved")

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage to stdout and exit 0 (argparse default).

        ``-h/--help`` is the only flag argparse owns; every other ``--`` token belongs to the opaque blob.
        """
        with pytest.raises(SystemExit) as exc:
            extract_json_field.main(["--help"])
        assert (exc.value.code, "usage" in capsys.readouterr().out.lower()) == (0, True)
