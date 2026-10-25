"""Execute shared audit snippets against count errors and reciprocal policy links."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_CHECKS = Path(__file__).resolve().parents[2] / "skills/audit/templates/checks-shared.md"
_BASH_REQUIRED = pytest.mark.skipif(
    not _bash_runs_posix_script() or any(shutil.which(tool) is None for tool in ("grep", "sed", "tr", "awk")),
    reason="Shared audit snippets require Bash executing POSIX scripts and text utilities",
)


@pytest.mark.integration
@_BASH_REQUIRED
@pytest.mark.parametrize(
    ("count", "status", "output"),
    [
        pytest.param("", 2, b"", id="unreadable-count"),
        pytest.param("0", 1, b"", id="no-examples"),
        pytest.param("2", 0, b"sample.md: 2 example blocks, 10 total lines\n", id="valid-count"),
    ],
)
def test_example_summary_preserves_count_status(count: str, status: int, output: bytes) -> None:
    """An empty grep count keeps the numeric-test diagnostic and status; valid counts keep their output."""
    line = next(line for line in _CHECKS.read_text(encoding="utf-8").splitlines() if "example blocks," in line)
    result = subprocess.run(
        ["bash", "-c", line],
        env={**os.environ, "count": count, "lines": "10", "f": "sample.md"},
        capture_output=True,
        check=False,
    )
    assert result.returncode == status, result.stderr
    assert result.stdout == output
    assert bool(result.stderr) == (count == "")


@pytest.mark.integration
@_BASH_REQUIRED
@pytest.mark.parametrize("path_style", ["repository", "plugin", "directory"])
def test_policy_siblings_require_the_declaring_file(tmp_path: Path, path_style: str) -> None:
    """A→B→C→B must report missing B→A, then accept the repaired reciprocal graph."""
    rules = tmp_path / "plugins/demo/rules"
    rules.mkdir(parents=True)
    prefix = {"repository": "plugins/demo/rules/", "plugin": "rules/", "directory": "./"}[path_style]
    a, b, c = (rules / f"{name}.md" for name in ("a", "b", "c"))
    a.write_text(f"<!-- policy-sibling: {prefix}b.md -->\n", encoding="utf-8")
    b.write_text(f"<!-- policy-sibling: {prefix}c.md -->\n", encoding="utf-8")
    c.write_text(f"<!-- policy-sibling: {prefix}b.md -->\n", encoding="utf-8")
    section = _CHECKS.read_text(encoding="utf-8").split("## Check 45 —", 1)[1]
    block = re.findall(r"```bash\n(.*?)\n```", section, re.DOTALL)[0]

    result = subprocess.run(["bash", "-c", block], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    sibling = "plugins/demo/rules/./b.md" if path_style == "directory" else "plugins/demo/rules/b.md"
    assert result.stdout.splitlines() == [
        "=== Check 45: Policy-sibling marker symmetry ===",
        f"⚠ 45-ASYMMETRIC: plugins/demo/rules/a.md — declares sibling {sibling}, "
        "but that file has no policy-sibling marker back",
    ]

    b.write_text(f"<!-- policy-sibling: {prefix}c.md, {prefix}a.md -->\n", encoding="utf-8")
    repaired = subprocess.run(["bash", "-c", block], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert repaired.returncode == 0, repaired.stderr
    assert repaired.stderr == ""
    assert repaired.stdout.splitlines() == [
        "=== Check 45: Policy-sibling marker symmetry ===",
        "✓: Check 45 — no unsynced policy-sibling markers found",
    ]
