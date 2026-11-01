"""Contract: the ``check-action-pins`` hook flags external GitHub Actions without an immutable pin and version comment.

A mutable tag or branch lets an upstream retag change what CI executes; the ``# vX.Y.Z`` comment keeps the pin
reviewable. Local ``./`` actions are exempt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _repo_checker_run import run_checker

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "check_action_pins.py"
_SHA = "0123456789abcdef0123456789abcdef01234567"


def _run(root: Path, workflow: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Write ``workflow`` as ``ci.yml`` below ``root`` and run the checker under ``root`` as the hook does."""
    directory = root / ".github" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ci.yml").write_text(workflow, encoding="utf-8", newline="\n")
    return run_checker(_SCRIPT, "--root", str(root), *args)


@pytest.mark.parametrize(
    ("line", "expected_rc"),
    [
        pytest.param(f"      - uses: actions/checkout@{_SHA} # v4.2.1", 0, id="pinned-with-version"),
        pytest.param(f"      - uses: actions/checkout@{_SHA} # v4", 0, id="pinned-with-major-version"),
        pytest.param("      - uses: actions/checkout@v4 # v4.2.1", 1, id="mutable-tag"),
        pytest.param("      - uses: actions/checkout@main", 1, id="branch-and-no-comment"),
        pytest.param(f"      - uses: actions/checkout@{_SHA[:39]} # v4.2.1", 1, id="short-sha"),
        pytest.param(f"      - uses: actions/checkout@{_SHA}", 1, id="missing-comment"),
        pytest.param(f"      - uses: actions/checkout@{_SHA} # latest", 1, id="comment-not-a-version"),
        pytest.param(f"      - uses: actions/checkout@{_SHA} # 4.2.1", 1, id="comment-without-v-prefix"),
    ],
)
def test_checker_requires_sha_pin_and_version_comment(tmp_path: Path, line: str, expected_rc: int) -> None:
    """An external action passes only with a 40-hex SHA pin and a ``vX[.Y[.Z]]`` comment after it."""
    result = _run(tmp_path, f"jobs:\n  build:\n    steps:\n{line}\n      - uses: ./local-action\n")

    assert result.returncode == expected_rc, result.stdout + result.stderr


def test_finding_names_workflow_line_and_fix(tmp_path: Path) -> None:
    """A mutable action is reported as ``path:line`` with the pin to apply."""
    result = _run(tmp_path, "steps:\n  - uses: actions/checkout@v4\n")

    assert result.returncode == 1
    assert ".github/workflows/ci.yml:2: mutable or malformed action actions/checkout@v4" in result.stdout


def test_local_actions_alone_make_the_scan_vacuous(tmp_path: Path) -> None:
    """A workflows directory without any external action fails, so a changed ``uses:`` syntax cannot pass unnoticed."""
    result = _run(tmp_path, "steps:\n  - uses: ./local-action\n")

    assert result.returncode == 1
    assert "no external action reference found" in result.stdout


def test_explicit_workflow_file_is_checked(tmp_path: Path) -> None:
    """A workflow named on the command line is checked without the vacuity rule of the whole-directory scan."""
    result = _run(tmp_path, "steps:\n  - uses: ./local-action\n", ".github/workflows/ci.yml")

    assert result.returncode == 0, result.stdout + result.stderr
