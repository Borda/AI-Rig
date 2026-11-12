"""Cover the diff-impact qualified-name adapter's bounds and fail-open input handling."""

import json
from collections.abc import Callable

import pytest
from extract_diff_impact_qnames import extract_qnames, main


@pytest.mark.parametrize("cap", [0, 1, 2])
def test_cli_caps_deduplicated_names(
    cap: int, feed_stdin: Callable[[str], None], capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI caps unique names after preserving their first-seen order."""
    feed_stdin('{"changed_modules":[{"changed_symbols":["a::f","a::f","b::g","c::h"]}]}')
    assert main(["--cap", str(cap)]) == 0
    assert capsys.readouterr().out.splitlines() == ["a::f", "b::g"][:cap]


@pytest.mark.parametrize("raw", ["", "not JSON", "{", "null", "[]", '{"changed_modules":null}'])
def test_malformed_input_emits_nothing(
    raw: str, feed_stdin: Callable[[str], None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Unreadable JSON or an unexpected top-level shape leaves the caller's query loop empty."""
    feed_stdin(raw)
    assert main([]) == 0
    assert capsys.readouterr().out == ""


def test_default_cap_preserves_first_seen_order() -> None:
    """The default bound keeps twelve names in input order across changed modules."""
    names = [f"module::fn{index}" for index in range(14)]
    payload = {"changed_modules": [{"changed_symbols": names[:8]}, {"changed_symbols": names[6:]}]}
    assert extract_qnames(json.dumps(payload)) == names[:12]
