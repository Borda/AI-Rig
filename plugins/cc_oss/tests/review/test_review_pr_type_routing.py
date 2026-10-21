"""Tests that ``/oss:review`` routes each changed-file mix to the reviewers able to judge it.

Step 0 classifies a PR as ``DOCS_TYPING``, ``TESTS_CI`` or ``CODE`` and the two narrow types skip the CI status
challenge, the challenger and most specialists. File scope detection then narrows ``CODE`` PRs into the docs-only,
CI/CD-only and docs+CI/CD lineups. A GitHub Actions change classified as ``TESTS_CI`` never reached ``oss:cicd-
steward``, and a requirements manifest or notebook classified as docs never reached the qa/OWASP reviewer; these tests
run the shipped bash blocks on representative file lists to keep both routes closed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REVIEW = Path(__file__).resolve().parents[2] / "skills" / "review"
_STEP0_BLOCK = next(
    block
    for block in re.findall(r"```bash\n(.*?)```", (_REVIEW / "SKILL.md").read_text(encoding="utf-8"), re.DOTALL)
    if "_TEST_CI_COUNT=" in block
)
_SCOPE_BLOCK = next(
    block
    for block in re.findall(
        r"```bash\n(.*?)```", (_REVIEW / "modes" / "scope-detection.md").read_text(encoding="utf-8"), re.DOTALL
    )
    if "OTHER_CODE_FILES=" in block
)
_PR = "7"


def _run(block: str, tmp_path: Path, changed: list[str]) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run one review bash block against a fake PR snapshot and return its process plus persisted mode flags."""
    snap = tmp_path / "snap"
    snap.mkdir(exist_ok=True)
    listing = "".join(f"{path}\n" for path in changed)
    (snap / "files.txt").write_text(listing, encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-changed-files-test").write_text(listing, encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-pr-tag-test").write_text(f"{_PR}\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-snap-dir-test").write_text(f"{snap}\n", encoding="utf-8", newline="\n")
    (tmp_path / "oss-review-flags-test").write_text("DIRECT_PATH_MODE=false\n", encoding="utf-8", newline="\n")
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test"})
    completed = subprocess.run(
        [shutil.which("bash"), "-c", block], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False
    )
    flags_file = tmp_path / f"oss-review-mode-flags-{_PR}-test"
    flags = {}
    if flags_file.exists():
        flags = dict(line.split("=", 1) for line in flags_file.read_text(encoding="utf-8").splitlines() if "=" in line)
    return completed, flags


@pytest.mark.skipif(shutil.which("bash") is None, reason="Review skill blocks run in Bash")
@pytest.mark.parametrize(
    ("changed", "expected_type"),
    [
        pytest.param([".github/workflows/ci.yml"], "CODE", id="actions-only-is-not-tests-ci"),
        pytest.param(["tests/test_x.py", ".github/workflows/ci.yml"], "CODE", id="tests-plus-actions-is-code"),
        pytest.param([".pre-commit-config.yaml"], "CODE", id="yaml-config-is-code"),
        pytest.param(["tests/test_x.py", "tox.ini"], "TESTS_CI", id="tests-and-tox-stay-tests-ci"),
        pytest.param(["requirements.txt"], "CODE", id="requirements-manifest-is-code"),
        pytest.param(["notebooks/demo.ipynb"], "CODE", id="notebook-is-code"),
        pytest.param(["README.md", "src/pkg/x.pyi"], "DOCS_TYPING", id="docs-and-stubs-stay-docs-typing"),
    ],
)
def test_step0_pr_type(tmp_path: Path, changed: list[str], expected_type: str) -> None:
    """Only test-only and docs/stub-only PRs take the narrow Step 0 lineups; CI config, manifests, notebooks stay CODE.

    The narrow types switch off the challenger and skip File scope detection, so anything carrying CI, dependency or
    executable risk must stay ``CODE`` to reach ``oss:cicd-steward`` or the qa/OWASP reviewer.
    """
    completed, flags = _run(_STEP0_BLOCK, tmp_path, changed)
    assert (completed.returncode, flags.get("PR_TYPE")) == (0, expected_type)


@pytest.mark.skipif(shutil.which("bash") is None, reason="Review skill blocks run in Bash")
@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        pytest.param([".github/workflows/ci.yml"], ("true", "false", "false"), id="actions-only-reaches-cicd-steward"),
        pytest.param(["README.md", ".github/workflows/ci.yml"], ("false", "false", "true"), id="docs-plus-actions"),
        pytest.param(["docs/index.md"], ("false", "true", "false"), id="docs-only"),
        pytest.param(["requirements.txt"], ("false", "false", "false"), id="manifest-gets-code-lineup"),
        pytest.param(["notebooks/demo.ipynb", "README.md"], ("false", "false", "false"), id="notebook-not-docs-only"),
        pytest.param([".pre-commit-config.yaml"], ("false", "false", "false"), id="yaml-config-gets-code-lineup"),
        pytest.param(
            ["pyproject.toml", ".github/workflows/ci.yml"], ("false", "false", "false"), id="manifest-not-cicd-only"
        ),
    ],
)
def test_scope_detection_lineup(tmp_path: Path, changed: list[str], expected: tuple[str, str, str]) -> None:
    """Notebook, manifest and config changes never collapse into a docs- or CI-only lineup, and are never skipped.

    The tuple is (``CICD_ONLY_MODE``, ``DOCS_ONLY_MODE``, ``DOCS_CICD_MODE``); all three false means the full code
    lineup, where CHORE scope keeps qa-specialist for dependency manifests and notebooks.
    """
    completed, flags = _run(_SCOPE_BLOCK, tmp_path, changed)
    observed = (flags.get("CICD_ONLY_MODE"), flags.get("DOCS_ONLY_MODE"), flags.get("DOCS_CICD_MODE"))
    assert (completed.returncode, observed) == (0, expected)


@pytest.mark.skipif(shutil.which("bash") is None, reason="Review skill blocks run in Bash")
def test_scope_detection_skips_only_unreviewable_changes(tmp_path: Path) -> None:
    """A PR touching only binary assets is skipped with an explicit message and no mode flags.

    The skip branch must stay reachable for changes no reviewer can judge, while every file class Step 0 keeps as
    ``CODE`` is excluded from it (covered by the lineup cases above).
    """
    completed, flags = _run(_SCOPE_BLOCK, tmp_path, ["assets/logo.png"])
    assert (completed.returncode, "skipping review" in completed.stdout, flags) == (0, True, {})
