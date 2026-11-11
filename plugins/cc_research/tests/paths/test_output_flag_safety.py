"""Output flag parsing preserves shell-token boundaries and validates fallback paths."""

from __future__ import annotations

from pathlib import Path

import check_output_within_root as output
import pytest


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param('"Discuss --out elsewhere.md" --out actual.md', "actual.md", id="quoted-goal"),
        pytest.param("goal --without x.md", "", id="substring-is-not-flag"),
        pytest.param("goal prefix--out x.md", "", id="suffix-is-not-flag"),
        pytest.param("goal -- --out x.md", "", id="end-of-options"),
        pytest.param('goal --out "docs/my program.md"', "docs/my program.md", id="quoted-output"),
        pytest.param("goal --out=docs/program.md", "docs/program.md", id="attached-value"),
        pytest.param("improve the model's accuracy --team", "", id="apostrophe-goal"),
        pytest.param("improve the model's accuracy --out d/p.md", "d/p.md", id="apostrophe-goal-with-out"),
        pytest.param(r"goal --out docs\program.md", r"docs\program.md", id="backslash-path"),
    ],
)
def test_only_output_flag_tokens_are_parsed(arguments: str, expected: str) -> None:
    """Goal prose containing the flag spelling does not redirect the output."""
    assert output.parse_out_flag(arguments) == expected


def test_default_output_outside_root_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent explicit flag does not bypass confinement for the default value."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "output-test")
    monkeypatch.setattr(output, "project_root", lambda timeout: str(tmp_path))

    assert (
        output.main(
            ["--parse-out=goal", "--sentinel", "sweep-out-path", "--default-out", str(tmp_path.parent / "escaped.md")]
        )
        == 2
    )
    assert not (tmp_path / "sweep-out-path-output-test").exists()


@pytest.mark.parametrize("arguments", ["goal --out", "goal --out --team"])
def test_malformed_output_flag_is_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: str) -> None:
    """Missing or malformed flag values do not silently persist a fallback path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "output-test")

    assert output.main([f"--parse-out={arguments}", "--sentinel", "sweep-out-path"]) == 2
    assert not (tmp_path / "sweep-out-path-output-test").exists()
