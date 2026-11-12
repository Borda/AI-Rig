"""Exercise bounded, linear target extraction through the public parsers and CLIs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import derive_codemap_target
import parse_target_qname
import pytest

_GENERATED_GOAL_CLI = (
    "import runpy,sys; from pathlib import Path; script=sys.argv[1]; "
    "sys.path.insert(0,str(Path(script).parent)); "
    "sys.argv=[script,*sys.argv[2:-2],sys.argv[-2]*int(sys.argv[-1])]; "
    "runpy.run_path(script,run_name='__main__')"
)


@pytest.mark.parametrize("parser", [derive_codemap_target.derive_target, parse_target_qname.split_qname])
def test_rejects_oversized_arguments(parser) -> None:
    """Oversized prose is rejected rather than scanned or persisted."""
    with pytest.raises(ValueError, match="65536"):
        parser(" " * 65537)


@pytest.mark.parametrize("script", ["derive_codemap_target.py", "parse_target_qname.py"])
def test_near_match_finishes_within_bounded_time(script: str, tmp_path: Path) -> None:
    """An identifier without a delimiter cannot restart a search at every character."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _GENERATED_GOAL_CLI,
            str(Path(derive_codemap_target.__file__).parent / script),
            "a",
            "60000",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
        env={**os.environ, "TMPDIR": str(tmp_path), "CSID": "bounded"},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        pytest.param("123pkg.mod::fn then a.b", ("pkg.mod", "fn"), id="numeric-prefix"),
        pytest.param("a.1b.c", ("b.c", ""), id="invalid-middle-segment"),
        pytest.param("123.a.b", ("a.b", ""), id="numeric-segment"),
        pytest.param("a.b then 12..pkg..::fn", ("pkg..", "fn"), id="qualified-priority"),
    ],
)
def test_preserves_substring_extraction(goal: str, expected: tuple[str, str]) -> None:
    """Linear scanning retains the existing leading-digit and malformed-segment behavior."""
    assert derive_codemap_target.derive_target(goal) == expected


@pytest.mark.parametrize("goal", ["1::a::b", "..::a::b", "1::2::a::b", "1::a.b::fn"])
def test_invalid_candidate_does_not_consume_next_qualified_name(goal: str) -> None:
    """A numeric-only module does not hide a valid name starting at its apparent function."""
    expected = ("a.b", "fn") if ".b" in goal else ("a", "b")
    assert derive_codemap_target.derive_target(goal) == expected
    assert parse_target_qname.split_qname(goal) == (*expected, "::".join(expected))


@pytest.mark.parametrize("script", ["derive_codemap_target.py", "parse_target_qname.py"])
def test_oversized_cli_does_not_write_state(script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI rejects over-limit arguments before changing any session state."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "bounded")
    argv = ["--keep-state", "target"] if script.startswith("derive") else ["--"]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _GENERATED_GOAL_CLI,
            str(Path(derive_codemap_target.__file__).parent / script),
            *argv,
            " ",
            "65537",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "65536" in result.stderr
    assert list(tmp_path.iterdir()) == []
