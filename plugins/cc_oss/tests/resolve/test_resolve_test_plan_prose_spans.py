"""Contributor-doc prose that merely names pytest never becomes the targeted or full-suite test command.

QA agents once received a targeted command that began with ``'pytest.param(...,' 'id=...)'``: an inline code span from a
contributor guide's parametrization advice was taken as the documented test runner. These cases pin that such spans are
skipped and the real documented runner (or the fallback) is used.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import resolve_test_plan as rtp
from _process import requires_git

#: Contributor-guide lines that mention pytest inside a code span without being a command.
_PARAM_ADVICE = "- Plain values stay bare; `pytest.param(..., id=...)` only for unreadable defaults.\n"
_PARAM_ADVICE_SLUG = "- Name cases with `pytest.param(..., id=semantic-slug)`.\n"


def _write(root: Path, relative: str, text: str) -> None:
    """Write one fixture file, creating parent directories."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.mark.parametrize(
    ("guide", "expected_command", "expected_runner"),
    [
        pytest.param(
            _PARAM_ADVICE + "- `make test` runs the suite.\n", "make test", "python -m pytest", id="advice-then-make"
        ),
        pytest.param(
            _PARAM_ADVICE_SLUG + "\n```bash\nuv run pytest -n 2 tests\n```\n",
            "uv run pytest -n 2 tests",
            "uv run pytest -n 2",
            id="advice-then-fenced-runner",
        ),
        pytest.param(_PARAM_ADVICE, "python -m pytest", "python -m pytest", id="advice-only"),
    ],
)
def test_parametrization_advice_is_never_the_runner(
    tmp_path: Path, guide: str, expected_command: str, expected_runner: str
) -> None:
    """A ``pytest.param(...)`` code span is skipped; the documented runner or the fallback is chosen instead."""
    (tmp_path / "tests").mkdir()
    _write(tmp_path, "AGENTS.md", guide)
    found = rtp.full_command(tmp_path)
    assert (found["command"], found["runner"]) == (expected_command, expected_runner)


@requires_git
def test_targeted_command_starts_with_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The targeted plan's command is the runner followed by test paths, with no stray prose tokens."""
    _write(tmp_path, "AGENTS.md", _PARAM_ADVICE + "- `make test` runs the suite.\n")
    _write(tmp_path, "pkg/core.py", "def run():\n    return 1\n")
    _write(tmp_path, "tests/test_core.py", "from pkg.core import run\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "AGENTS.md", "pkg/core.py", "tests/test_core.py"], cwd=tmp_path, check=True)
    monkeypatch.setattr(rtp.shutil, "which", lambda _name: None)
    monkeypatch.chdir(tmp_path)

    assert rtp.main(["targeted", "--files", "pkg/core.py"]) == 0

    command = json.loads(capsys.readouterr().out)["command"]
    assert command == "python -m pytest tests/test_core.py"
    assert "pytest.param" not in command
