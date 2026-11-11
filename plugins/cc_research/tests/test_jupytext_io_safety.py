"""Cover notebook gate errors and preservation of source newline bytes."""

from __future__ import annotations

from pathlib import Path

import fix_jupytext_blank_md as gate
import pytest


def test_check_rejects_a_batch_without_examined_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail report-only validation when all supplied paths have unsupported suffixes."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "notebook.txt"
    target.write_text("unchanged", encoding="utf-8")

    assert gate.main(["--check", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize("check", [False, True])
def test_read_error_does_not_abort_remaining_notebooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], check: bool
) -> None:
    """Report an unreadable input while still examining the following valid notebook."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "good.py"
    original = "# %% [markdown]\n# text\n#\n"
    target.write_text(original, encoding="utf-8")
    options = ["--check"] if check else []

    assert gate.main([*options, str(tmp_path / "missing.py"), str(target)]) == 1

    captured = capsys.readouterr()
    assert "cannot read" in captured.err
    assert str(target) in captured.out
    assert target.read_text(encoding="utf-8") == (original if check else "# %% [markdown]\n# text\n\n")


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        pytest.param(b"# %% [markdown]\r\n# text\r\n#\r\n", b"# %% [markdown]\r\n# text\r\n\r\n", id="crlf"),
        pytest.param(b"# %% [markdown]\n# text\r\n#\r\n", b"# %% [markdown]\n# text\r\n\r\n", id="mixed"),
    ],
)
def test_notebook_rewrite_preserves_newline_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: bytes, expected: bytes
) -> None:
    """Clear only the hash spacer while preserving each original line terminator."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "notebook.py"
    target.write_bytes(original)

    assert gate.main([str(target)]) == 0
    assert target.read_bytes() == expected
